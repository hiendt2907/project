"""TDD — workers.siem_correlation_loop: consume omni-siem-raw → passthrough
incident envelope + graph correlate → emit chain. Mirror error semantics của
brain-go ``app.runKafka`` (produce/correlate lỗi chỉ log, không chặn consume).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis

from workers.siem_correlation_loop import _handle_raw_message


class _KafkaCapture:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict, bytes | None]] = []

    async def send_dict(self, topic: str, envelope: dict, *, key: bytes | None = None) -> None:
        self.sent.append((topic, envelope, key))


class _FailingKafka(_KafkaCapture):
    def __init__(self, fail_topics: set[str]) -> None:
        super().__init__()
        self._fail = fail_topics

    async def send_dict(self, topic: str, envelope: dict, *, key: bytes | None = None) -> None:
        if topic in self._fail:
            raise RuntimeError(f"kafka down for {topic}")
        await super().send_dict(topic, envelope, key=key)


def _ctx(kafka) -> SimpleNamespace:
    return SimpleNamespace(
        kafka=kafka,
        redis=FakeRedis(decode_responses=True),
        ledger=SimpleNamespace(record_exception=AsyncMock()),
    )


def _msg(value: bytes | dict) -> SimpleNamespace:
    if isinstance(value, dict):
        value = json.dumps(value).encode()
    return SimpleNamespace(value=value, partition=0, offset=7)


def _raw(id_: str, category: str = "auth_failure", ip: str = "10.9.9.9") -> dict:
    return {
        "id": id_, "tenant_id": "acme", "severity": "high", "source": "finguard",
        "category": category, "timestamp_unix": 1721600000, "schema_version": "1.0.0",
        "source_ip": ip, "description": f"event {id_}",
    }


class _Correlator:
    """Stub correlator with the same .process signature."""

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    async def process(self, inc, *, now=None):
        self.calls.append(inc.incident_id)
        if self.error:
            raise self.error
        return self.result


class TestHandleRawMessage:
    async def test_invalid_message_is_dropped_no_sends(self):
        kafka = _KafkaCapture()
        corr = _Correlator()
        await _handle_raw_message(_ctx(kafka), corr, _msg(b"{broken"))
        assert kafka.sent == []
        assert corr.calls == []

    async def test_valid_message_passthrough_to_incidents_topic(self):
        kafka = _KafkaCapture()
        corr = _Correlator()
        await _handle_raw_message(_ctx(kafka), corr, _msg(_raw("i-1")))
        assert len(kafka.sent) == 1
        topic, env, key = kafka.sent[0]
        assert topic == "omni-siem-incidents"
        assert env["id"] == "i-1"
        assert key == b"i-1"
        assert corr.calls == ["i-1"]

    async def test_chain_emitted_with_chain_id_key(self):
        kafka = _KafkaCapture()
        chain = {"chain_id": "c-1", "tenant_id": "acme", "member_events": [{}, {}]}
        corr = _Correlator(result=chain)
        await _handle_raw_message(_ctx(kafka), corr, _msg(_raw("i-2")))
        assert len(kafka.sent) == 2
        topic, env, key = kafka.sent[1]
        assert topic == "omni-siem-chains"
        assert env is chain
        assert key == b"c-1"

    async def test_incident_produce_failure_does_not_block_correlation(self):
        kafka = _FailingKafka({"omni-siem-incidents"})
        chain = {"chain_id": "c-2", "tenant_id": "acme"}
        corr = _Correlator(result=chain)
        ctx = _ctx(kafka)
        await _handle_raw_message(ctx, corr, _msg(_raw("i-3")))
        # correlation still ran and the chain still went out
        assert corr.calls == ["i-3"]
        assert [t for t, _, _ in kafka.sent] == ["omni-siem-chains"]
        ctx.ledger.record_exception.assert_awaited()

    async def test_correlator_failure_is_swallowed(self):
        kafka = _KafkaCapture()
        corr = _Correlator(error=RuntimeError("redis down"))
        ctx = _ctx(kafka)
        await _handle_raw_message(ctx, corr, _msg(_raw("i-4")))
        assert [t for t, _, _ in kafka.sent] == ["omni-siem-incidents"]
        ctx.ledger.record_exception.assert_awaited()

    async def test_env_override_topics(self, monkeypatch):
        monkeypatch.setenv("OMNI_SIEM_CORR_TOPIC_INCIDENTS", "inc-py")
        monkeypatch.setenv("OMNI_SIEM_CORR_TOPIC_CHAINS", "chain-py")
        kafka = _KafkaCapture()
        corr = _Correlator(result={"chain_id": "c-3"})
        await _handle_raw_message(_ctx(kafka), corr, _msg(_raw("i-5")))
        assert [t for t, _, _ in kafka.sent] == ["inc-py", "chain-py"]


class TestSettingsGate:
    def test_flag_defaults_off(self):
        from workers.settings import WorkerSettings

        ws = WorkerSettings(_env_file=None)
        assert ws.siem_correlation_enabled is False

    def test_flag_env_alias(self, monkeypatch):
        from workers.settings import WorkerSettings

        monkeypatch.setenv("OMNI_SIEM_CORRELATION_ENABLED", "true")
        ws = WorkerSettings(_env_file=None)
        assert ws.siem_correlation_enabled is True
