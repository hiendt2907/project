"""TDD for kafka_siem_chains_loop poison-retry fix.

Bug: the per-message try/except wrapped `parse_chain_message` +
`consumer_obj.handle_chain(chain)` and, on ANY exception (including a real
one-off Redis/Kafka blip surfacing through `write_audit_block`'s CRAT
fail-closed guarantee or `_emit`'s Kafka send), immediately committed the
offset after a SINGLE failed attempt -- permanently dropping a correlated
attack chain (lateral_movement, data_exfil, ...) with only a log line and no
retry, no tombstone. The three sibling loops in the same file
(`kafka_evidence_loop`, `kafka_discovery_evidence_loop`,
`kafka_knowledge_evidence_loop`) all retry up to `max_poison_retries` times
with a sleep between attempts before poison-acking.

These tests exercise the REAL production functions (`ChainConsumer.handle_chain`,
`parse_chain_message`, `write_audit_block` via the real chain_consumer module)
with deterministic failure-injection stubs standing in for Redis/Kafka -- not
reimplemented retry logic.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from services.analyst.chain_consumer import ChainConsumer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chain_dict(chain_id: str = "11111111-1111-1111-1111-111111111111") -> dict:
    return {
        "chain_id": chain_id,
        "tenant_id": "acme",
        "attack_category": "lateral_movement",
        "kill_chain_stage": "lateral_movement",
        "kill_chain_ordered": True,
        "confidence": 0.82,
        "signals": {"entity": 1.0, "sequence": 0.5, "volume": 0.5, "confidence": 0.82},
        "common_dimensions": [{"type": "ip", "value": "203.0.113.7"}],
        "member_events": [
            {"incident_id": "e1", "category": "auth_failure", "kill_chain_stage": "initial_access", "kill_chain_order": 2},
            {"incident_id": "e2", "category": "new_process", "kill_chain_stage": "execution", "kill_chain_order": 3},
        ],
        "schema_version": "1.0.0",
    }


def _chain_msg(chain_id: str = "11111111-1111-1111-1111-111111111111", *, partition: int = 0, offset: int = 42):
    import json as _json

    return SimpleNamespace(value=_json.dumps(_chain_dict(chain_id)).encode(), partition=partition, offset=offset)


class _FlakyKafka:
    """Real-shaped kafka.send_dict stub: raises a transient error for the
    first ``fail_times`` calls, then succeeds -- simulating a one-off Kafka
    blip hit during CRAT audit publish or advisory emit."""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls = 0
        self.fail_times = fail_times

    async def send_dict(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("kafka blip (simulated)")


class _AlwaysRaisingPipeline:
    def __getattr__(self, _name: str):
        return lambda *a, **kw: self

    async def execute(self):
        raise ConnectionError("redis blip (simulated)")


class _AlwaysRaisingRedis:
    """Simulates a real Redis outage during write_audit_block's pipeline
    step -- distinct from a legitimate empty/missing-key read."""

    def pipeline(self):
        return _AlwaysRaisingPipeline()


def _ctx(redis_client: Any, kafka: Any) -> SimpleNamespace:
    ledger = MagicMock()
    ledger.record_exception = AsyncMock()
    return SimpleNamespace(
        settings=SimpleNamespace(kafka_topic_actions="omni-actions", embed_model="nomic-embed-text"),
        redis=redis_client,
        llm=None,  # short-circuits cohesion + recall -> pure heuristic path, no network calls
        vector_store=None,
        ledger=ledger,
        kafka=kafka,
    )


def _real_ctx() -> SimpleNamespace:
    return _ctx(fakeredis.aioredis.FakeRedis(decode_responses=True), _FlakyKafka(fail_times=0))


# ---------------------------------------------------------------------------
# Env-driven config (no hardcoded threshold)
# ---------------------------------------------------------------------------

class TestSiemChainMaxPoisonRetries:
    def test_default_when_unset(self, monkeypatch):
        from workers.omni_worker import siem_chain_max_poison_retries

        monkeypatch.delenv("OMNI_SIEM_CHAIN_MAX_POISON_RETRIES", raising=False)
        assert siem_chain_max_poison_retries() == 3

    def test_reads_env_override(self, monkeypatch):
        from workers.omni_worker import siem_chain_max_poison_retries

        monkeypatch.setenv("OMNI_SIEM_CHAIN_MAX_POISON_RETRIES", "5")
        assert siem_chain_max_poison_retries() == 5

    def test_falls_back_on_invalid_or_negative(self, monkeypatch):
        from workers.omni_worker import siem_chain_max_poison_retries

        monkeypatch.setenv("OMNI_SIEM_CHAIN_MAX_POISON_RETRIES", "not-a-number")
        assert siem_chain_max_poison_retries() == 3
        monkeypatch.setenv("OMNI_SIEM_CHAIN_MAX_POISON_RETRIES", "-1")
        assert siem_chain_max_poison_retries() == 3

    def test_zero_is_a_valid_override(self, monkeypatch):
        """0 retries (poison-ack on first failure) must be an explicit, honored choice."""
        from workers.omni_worker import siem_chain_max_poison_retries

        monkeypatch.setenv("OMNI_SIEM_CHAIN_MAX_POISON_RETRIES", "0")
        assert siem_chain_max_poison_retries() == 0


class TestSiemChainPoisonRetrySleepS:
    def test_default_when_unset(self, monkeypatch):
        from workers.omni_worker import siem_chain_poison_retry_sleep_s

        monkeypatch.delenv("OMNI_SIEM_CHAIN_POISON_RETRY_SLEEP_S", raising=False)
        assert siem_chain_poison_retry_sleep_s() == 0.5

    def test_reads_env_override(self, monkeypatch):
        from workers.omni_worker import siem_chain_poison_retry_sleep_s

        monkeypatch.setenv("OMNI_SIEM_CHAIN_POISON_RETRY_SLEEP_S", "1.5")
        assert siem_chain_poison_retry_sleep_s() == 1.5

    def test_falls_back_on_invalid_or_negative(self, monkeypatch):
        from workers.omni_worker import siem_chain_poison_retry_sleep_s

        monkeypatch.setenv("OMNI_SIEM_CHAIN_POISON_RETRY_SLEEP_S", "nope")
        assert siem_chain_poison_retry_sleep_s() == 0.5
        monkeypatch.setenv("OMNI_SIEM_CHAIN_POISON_RETRY_SLEEP_S", "-2")
        assert siem_chain_poison_retry_sleep_s() == 0.5


# ---------------------------------------------------------------------------
# _process_siem_chain_message -- the extracted, testable retry unit used by
# kafka_siem_chains_loop's `async for msg in consumer` body.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestProcessSiemChainMessage:
    async def test_success_commits_once_no_tombstone(self, monkeypatch):
        from workers.omni_worker import _process_siem_chain_message

        hc_record = MagicMock()
        monkeypatch.setattr("workers.omni_worker._hc_record_msg", hc_record)
        tombstone = AsyncMock()
        monkeypatch.setattr("workers.omni_worker.emit_terminal_tombstone", tombstone)

        ctx = _real_ctx()
        consumer_obj = ChainConsumer(ctx)
        consumer = SimpleNamespace(commit=AsyncMock())
        msg = _chain_msg()

        await _process_siem_chain_message(ctx, consumer, consumer_obj, msg)

        assert consumer.commit.await_count == 1
        hc_record.assert_called_once()
        tombstone.assert_not_called()
        ctx.ledger.record_exception.assert_not_called()

    async def test_invalid_json_is_committed_without_retry(self, monkeypatch):
        """A message that isn't a chain at all (decode failure) must still be
        acked -- this is not the poison-retry path, matches prior behavior."""
        from workers.omni_worker import _process_siem_chain_message

        monkeypatch.setattr("workers.omni_worker._hc_record_msg", MagicMock())
        tombstone = AsyncMock()
        monkeypatch.setattr("workers.omni_worker.emit_terminal_tombstone", tombstone)

        ctx = _real_ctx()
        consumer_obj = ChainConsumer(ctx)
        consumer = SimpleNamespace(commit=AsyncMock())
        msg = SimpleNamespace(value=b"not json", partition=0, offset=1)

        await _process_siem_chain_message(ctx, consumer, consumer_obj, msg)

        assert consumer.commit.await_count == 1
        tombstone.assert_not_called()

    async def test_transient_kafka_blip_retries_then_succeeds(self, monkeypatch):
        """One-off Kafka blip during CRAT audit publish must NOT drop the
        chain -- the loop should retry and succeed, committing only once,
        after the retry succeeds."""
        from workers.omni_worker import _process_siem_chain_message

        monkeypatch.setattr("workers.omni_worker._hc_record_msg", MagicMock())
        sleeps: list[float] = []

        async def _fake_sleep(s: float) -> None:
            sleeps.append(s)

        monkeypatch.setattr("workers.omni_worker.asyncio.sleep", _fake_sleep)
        tombstone = AsyncMock()
        monkeypatch.setattr("workers.omni_worker.emit_terminal_tombstone", tombstone)

        kafka = _FlakyKafka(fail_times=1)
        ctx = _ctx(fakeredis.aioredis.FakeRedis(decode_responses=True), kafka)
        consumer_obj = ChainConsumer(ctx)
        consumer = SimpleNamespace(commit=AsyncMock())
        msg = _chain_msg()

        await _process_siem_chain_message(ctx, consumer, consumer_obj, msg)

        assert consumer.commit.await_count == 1, "must commit exactly once, only after the retry succeeds"
        assert ctx.ledger.record_exception.await_count == 1, "one failed attempt must be recorded"
        assert len(sleeps) == 1, "must sleep between the failed attempt and the retry"
        tombstone.assert_not_called()

    async def test_exhausted_retries_poison_acks_with_tombstone(self, monkeypatch):
        """A permanently-failing dependency (not transient) must still
        eventually be acked (to avoid infinite reprocessing) but ONLY after
        exhausting retries, and it must leave an observable tombstone --
        never a silent drop."""
        from workers.omni_worker import _process_siem_chain_message

        monkeypatch.setattr("workers.omni_worker._hc_record_msg", MagicMock())

        async def _fake_sleep(_s: float) -> None:
            return None

        monkeypatch.setattr("workers.omni_worker.asyncio.sleep", _fake_sleep)
        tombstone = AsyncMock()
        monkeypatch.setattr("workers.omni_worker.emit_terminal_tombstone", tombstone)
        monkeypatch.setenv("OMNI_SIEM_CHAIN_MAX_POISON_RETRIES", "3")

        ctx = _ctx(_AlwaysRaisingRedis(), _FlakyKafka(fail_times=0))
        consumer_obj = ChainConsumer(ctx)
        consumer = SimpleNamespace(commit=AsyncMock())
        chain_id = "22222222-2222-2222-2222-222222222222"
        msg = _chain_msg(chain_id=chain_id, partition=2, offset=99)

        await _process_siem_chain_message(ctx, consumer, consumer_obj, msg)

        assert ctx.ledger.record_exception.await_count == 4, "3 retries + the original attempt = 4"
        assert consumer.commit.await_count == 1, "commit only once, after retries are exhausted"
        tombstone.assert_awaited_once()
        _, tomb_kwargs = tombstone.call_args
        assert tomb_kwargs["trace_id"] == chain_id
        assert tomb_kwargs["reason_code"] == "SIEM_CHAIN_CONSUMER_POISON"
        assert tomb_kwargs["component"] == "kafka_siem_chains_loop"
        assert "partition=2" in tomb_kwargs["detail"]
        assert "offset=99" in tomb_kwargs["detail"]

    async def test_env_override_shrinks_retry_budget(self, monkeypatch):
        """max_poison_retries=1 must poison-ack after 2 total attempts, not 4."""
        from workers.omni_worker import _process_siem_chain_message

        monkeypatch.setattr("workers.omni_worker._hc_record_msg", MagicMock())

        async def _fake_sleep(_s: float) -> None:
            return None

        monkeypatch.setattr("workers.omni_worker.asyncio.sleep", _fake_sleep)
        tombstone = AsyncMock()
        monkeypatch.setattr("workers.omni_worker.emit_terminal_tombstone", tombstone)
        monkeypatch.setenv("OMNI_SIEM_CHAIN_MAX_POISON_RETRIES", "1")

        ctx = _ctx(_AlwaysRaisingRedis(), _FlakyKafka(fail_times=0))
        consumer_obj = ChainConsumer(ctx)
        consumer = SimpleNamespace(commit=AsyncMock())
        msg = _chain_msg()

        await _process_siem_chain_message(ctx, consumer, consumer_obj, msg)

        assert ctx.ledger.record_exception.await_count == 2
        assert consumer.commit.await_count == 1
        tombstone.assert_awaited_once()

    async def test_tombstone_emit_failure_does_not_block_poison_ack(self, monkeypatch):
        """If emit_terminal_tombstone itself fails, the message must still be
        committed (never re-enter poison reprocessing forever)."""
        from workers.omni_worker import _process_siem_chain_message

        monkeypatch.setattr("workers.omni_worker._hc_record_msg", MagicMock())

        async def _fake_sleep(_s: float) -> None:
            return None

        monkeypatch.setattr("workers.omni_worker.asyncio.sleep", _fake_sleep)
        monkeypatch.setattr(
            "workers.omni_worker.emit_terminal_tombstone",
            AsyncMock(side_effect=RuntimeError("tombstone emit blew up")),
        )
        monkeypatch.setenv("OMNI_SIEM_CHAIN_MAX_POISON_RETRIES", "0")

        ctx = _ctx(_AlwaysRaisingRedis(), _FlakyKafka(fail_times=0))
        consumer_obj = ChainConsumer(ctx)
        consumer = SimpleNamespace(commit=AsyncMock())
        msg = _chain_msg()

        await _process_siem_chain_message(ctx, consumer, consumer_obj, msg)

        assert consumer.commit.await_count == 1
