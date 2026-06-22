"""Coverage tests for services.evidence_adapter.worker.

No unittest.mock: uses fakeredis.aioredis for the Redis side and a small
hand-rolled producer stub that satisfies the ``send_and_wait`` contract.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from typing import Any

import fakeredis.aioredis
import pytest

# worker.py guards at module import time on ADAPTER_REDIS_URL / OMNI_REDIS_URL.
os.environ.setdefault("ADAPTER_REDIS_URL", "redis://localhost:6379")

from services.evidence_adapter.protocol import EvidenceAdapter
from services.evidence_adapter.worker import (
    AdapterGeneratorWorker,
    _ensure_group,
)


# ── Hand-rolled adapter + producer (no unittest.mock) ─────────────────────────

class _StaticAdapter:
    """Returns a fixed envelope list per call."""

    def __init__(self, envelopes: list[dict[str, Any]] | None = None,
                 raises: Exception | None = None) -> None:
        self._envelopes = envelopes or [{
            "trace_id": "t-fake",
            "probe": "siem",
            "alert_rule": "R",
            "alert_hint": "H",
            "extracted_fact": {},
            "raw": "raw",
        }]
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def to_evidence(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append(event)
        if self._raises is not None:
            raise self._raises
        return list(self._envelopes)


class _FakeProducer:
    def __init__(self, fail_send: bool = False, fail_start: int = 0) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.started = False
        self.stopped = False
        self._fail_send = fail_send
        self._fail_start_remaining = fail_start

    async def start(self) -> None:
        if self._fail_start_remaining > 0:
            self._fail_start_remaining -= 1
            raise ConnectionError("kafka not ready")
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send_and_wait(self, topic: str, value: Any = None, **_kwargs) -> None:
        if self._fail_send:
            raise RuntimeError("kafka send failed")
        self.sent.append((topic, value))


# ── Constructor guards ────────────────────────────────────────────────────────

def test_worker_requires_protocol_compliant_adapter():
    with pytest.raises(TypeError, match="EvidenceAdapter"):
        AdapterGeneratorWorker(object())  # type: ignore[arg-type]


def test_worker_constructs_with_static_adapter():
    assert isinstance(_StaticAdapter(), EvidenceAdapter)
    worker = AdapterGeneratorWorker(_StaticAdapter())
    assert worker._redis_backoff == 2


# ── _ensure_group helper ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ensure_group_creates_stream_group():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await _ensure_group(r)
    groups = await r.xinfo_groups("stream:siem_evidence_raw")
    assert any(g.get("name") == "omni-evidence-adapters" for g in groups)


@pytest.mark.asyncio
async def test_ensure_group_swallows_duplicate_creation():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await _ensure_group(r)
    # Second call must NOT raise.
    await _ensure_group(r)


# ── _handle: success + error branches ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_emits_envelopes_and_acks():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await _ensure_group(r)
    msg_id = await r.xadd("stream:siem_evidence_raw", {"category": "ddos", "severity": "high"})

    adapter = _StaticAdapter()
    producer = _FakeProducer()
    worker = AdapterGeneratorWorker(adapter)
    await worker._handle(r, producer, msg_id, {"category": "ddos", "severity": "high"})

    assert len(producer.sent) == 1
    topic, value = producer.sent[0]
    assert topic == "omni-diagnostic-evidence"
    payload = json.loads(value["data"])
    assert payload["trace_id"] == "t-fake"
    # ack succeeded — xpending shows no pending
    pending = await r.xpending("stream:siem_evidence_raw", "omni-evidence-adapters")
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_handle_emits_multiple_envelopes():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await _ensure_group(r)
    adapter = _StaticAdapter(envelopes=[
        {"trace_id": "a", "probe": "p1", "alert_rule": "R", "alert_hint": "H", "extracted_fact": {}, "raw": ""},
        {"trace_id": "a", "probe": "p2", "alert_rule": "R", "alert_hint": "H", "extracted_fact": {}, "raw": ""},
    ])
    producer = _FakeProducer()
    worker = AdapterGeneratorWorker(adapter)
    msg_id = await r.xadd("stream:siem_evidence_raw", {"x": "1"})
    await worker._handle(r, producer, msg_id, {"x": "1"})
    assert len(producer.sent) == 2


@pytest.mark.asyncio
async def test_handle_skips_ack_on_producer_failure():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await _ensure_group(r)
    adapter = _StaticAdapter()
    producer = _FakeProducer(fail_send=True)
    worker = AdapterGeneratorWorker(adapter)
    msg_id = await r.xadd("stream:siem_evidence_raw", {"x": "1"})
    # Must read first so the message is "pending" under our consumer.
    await r.xreadgroup(
        groupname="omni-evidence-adapters",
        consumername="omni-evidence-adapter-1",
        streams={"stream:siem_evidence_raw": ">"},
        count=1,
        block=10,
    )
    await worker._handle(r, producer, msg_id, {"x": "1"})
    # Message must remain pending — no ack happened.
    pending = await r.xpending("stream:siem_evidence_raw", "omni-evidence-adapters")
    assert pending["pending"] == 1


@pytest.mark.asyncio
async def test_handle_skips_ack_when_adapter_raises():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await _ensure_group(r)
    adapter = _StaticAdapter(raises=ValueError("adapter blew up"))
    producer = _FakeProducer()
    worker = AdapterGeneratorWorker(adapter)
    msg_id = await r.xadd("stream:siem_evidence_raw", {"x": "1"})
    await r.xreadgroup(
        groupname="omni-evidence-adapters",
        consumername="omni-evidence-adapter-1",
        streams={"stream:siem_evidence_raw": ">"},
        count=1,
        block=10,
    )
    await worker._handle(r, producer, msg_id, {"x": "1"})
    pending = await r.xpending("stream:siem_evidence_raw", "omni-evidence-adapters")
    assert pending["pending"] == 1
    assert producer.sent == []


# ── _poll_once: success + empty + error branches ──────────────────────────────

@pytest.mark.asyncio
async def test_poll_once_handles_empty_stream():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await _ensure_group(r)
    adapter = _StaticAdapter()
    producer = _FakeProducer()
    worker = AdapterGeneratorWorker(adapter)
    # No messages on the stream — poll returns nothing, but must not raise
    # and must reset backoff on the successful XREADGROUP call.
    worker._redis_backoff = 16
    await worker._poll_once(r, producer)
    assert worker._redis_backoff == 2
    assert producer.sent == []


@pytest.mark.asyncio
async def test_poll_once_processes_messages():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await _ensure_group(r)
    await r.xadd("stream:siem_evidence_raw", {"category": "ddos"})
    await r.xadd("stream:siem_evidence_raw", {"category": "malware"})

    adapter = _StaticAdapter()
    producer = _FakeProducer()
    worker = AdapterGeneratorWorker(adapter)
    await worker._poll_once(r, producer)

    assert len(adapter.calls) == 2
    assert len(producer.sent) == 2


@pytest.mark.asyncio
async def test_poll_once_backs_off_on_redis_error(monkeypatch):
    class _BrokenRedis:
        async def xreadgroup(self, **_kwargs):
            raise ConnectionError("redis offline")

    # Avoid actually sleeping during backoff.
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    worker = AdapterGeneratorWorker(_StaticAdapter())
    assert worker._redis_backoff == 2
    await worker._poll_once(_BrokenRedis(), _FakeProducer())
    # Backoff grew but capped at 30
    assert worker._redis_backoff == 4
    assert sleeps and sleeps[0] == 2

    await worker._poll_once(_BrokenRedis(), _FakeProducer())
    assert worker._redis_backoff == 8

    # Drive it close to the cap to exercise the min(.., 30) branch.
    worker._redis_backoff = 25
    await worker._poll_once(_BrokenRedis(), _FakeProducer())
    assert worker._redis_backoff == 30


# ── Module-level env-guard ────────────────────────────────────────────────────

def test_module_raises_without_redis_url(monkeypatch):
    monkeypatch.delenv("ADAPTER_REDIS_URL", raising=False)
    monkeypatch.delenv("OMNI_REDIS_URL", raising=False)
    # Force a fresh import that re-runs the module body.
    sys.modules.pop("services.evidence_adapter.worker", None)
    try:
        with pytest.raises(RuntimeError, match="ADAPTER_REDIS_URL or OMNI_REDIS_URL"):
            importlib.import_module("services.evidence_adapter.worker")
    finally:
        # Restore so other tests still find a working module.
        sys.modules.pop("services.evidence_adapter.worker", None)
        os.environ["ADAPTER_REDIS_URL"] = "redis://localhost:6379"
        importlib.import_module("services.evidence_adapter.worker")


# ── main() with monkeypatched run() (avoid real Kafka/Redis I/O) ──────────────

@pytest.mark.asyncio
async def test_main_constructs_and_invokes_run(monkeypatch):
    from services.evidence_adapter import worker as worker_mod

    ran: list[bool] = []

    async def _stub_run(self):
        ran.append(True)

    monkeypatch.setattr(worker_mod.AdapterGeneratorWorker, "run", _stub_run)
    await worker_mod.main()
    assert ran == [True]


# ── run() with stubbed external clients ───────────────────────────────────────

@pytest.mark.asyncio
async def test_run_bootstraps_then_polls_then_terminates(monkeypatch):
    """Exercise the run() bootstrap retry + poll loop without real Kafka/Redis."""
    from services.evidence_adapter import worker as worker_mod

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    # Create the group first (id="$" = latest) so the message added after enrolment
    # is delivered when run() calls _poll_once.
    await _ensure_group(fake_redis)

    producer = _FakeProducer(fail_start=1)  # fail once, then succeed

    def _producer_factory(**_kwargs):
        return producer

    class _RedisFactory:
        @staticmethod
        def from_url(*_a, **_kw):
            return fake_redis

    monkeypatch.setattr(worker_mod, "AIOKafkaProducer", _producer_factory)
    monkeypatch.setattr(worker_mod, "Redis", _RedisFactory)

    # Skip sleeps during retry loops.
    async def _no_sleep(_):
        return None
    monkeypatch.setattr(worker_mod.asyncio, "sleep", _no_sleep)

    # Build a worker that terminates after a single successful poll.
    class _OneShotWorker(worker_mod.AdapterGeneratorWorker):
        def __init__(self, adapter):
            super().__init__(adapter)
            self._done = False

        async def _poll_once(self, redis, producer):
            # Inject a fresh message right before the parent polls so it appears
            # under the consumer group registered above with id="$" (latest).
            await redis.xadd("stream:siem_evidence_raw", {"category": "ddos"})
            await super()._poll_once(redis, producer)
            if not self._done:
                self._done = True
                raise asyncio.CancelledError

    worker = _OneShotWorker(_StaticAdapter())
    with pytest.raises(asyncio.CancelledError):
        await worker.run()

    assert producer.started is True
    assert producer.stopped is True
    assert len(producer.sent) >= 1
