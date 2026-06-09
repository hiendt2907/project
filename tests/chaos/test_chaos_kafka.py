"""Unit chaos tests — Kafka broker failure modes.

Tests graceful degradation: when Kafka send_dict fails, the worker logs
a CRITICAL error but does NOT raise or crash. The function returns False.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import fakeredis.aioredis
import pytest

from services.audit_ledger.signer import AuditLedgerError


class _KafkaCapture:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_dict(self, topic: str, payload: dict, **kwargs) -> None:
        self.sent.append((topic, payload))

    async def send_envelope_inner(self, topic: str, payload: dict) -> None:
        self.sent.append((topic, payload))

    async def close(self) -> None:
        pass


class _ErrorKafkaCapture(_KafkaCapture):
    """Kafka that raises ConnectionError when sending to the specified topic."""

    def __init__(self, fail_topic: str = "omni-actions") -> None:
        super().__init__()
        self._fail_topic = fail_topic

    async def send_dict(self, topic: str, payload: dict, **kwargs) -> None:
        if topic == self._fail_topic:
            raise ConnectionError(f"Kafka broker unreachable for topic {topic}")
        self.sent.append((topic, payload))

    async def send_envelope_inner(self, topic: str, payload: dict) -> None:
        if topic == self._fail_topic:
            raise ConnectionError(f"Kafka broker unreachable for topic {topic}")
        self.sent.append((topic, payload))


def _make_ctx(redis=None, kafka=None) -> SimpleNamespace:
    return SimpleNamespace(
        redis=redis or fakeredis.aioredis.FakeRedis(decode_responses=True),
        kafka=kafka or _KafkaCapture(),
        settings=SimpleNamespace(
            kafka_topic_actions="omni-actions",
            kafka_topic_audit_chain="omni-audit-chain",
        ),
    )


async def test_kafka_send_failure_returns_false_not_raises() -> None:
    """Kafka send_dict raising ConnectionError → emit_execute_mutate returns False, no exception propagated."""
    from workers.evidence_mutate_emit import emit_execute_mutate

    kafka = _ErrorKafkaCapture(fail_topic="omni-actions")
    ctx = _make_ctx(kafka=kafka)

    # write_audit_block succeeds (CRAT chain is written), but the Kafka action dispatch fails
    with patch("workers.evidence_mutate_emit.write_audit_block"):
        result = await emit_execute_mutate(
            ctx,
            trace="chaos-kafka-001",
            tool_name="k8s_rollout_restart",
            args={"namespace": "multi-agent", "deployment": "nginx-lab"},
        )

    # Must return False cleanly — worker must not crash
    assert result is False


async def test_kafka_audit_chain_send_failure_still_graceful() -> None:
    """If omni-audit-chain Kafka send fails inside write_audit_block, emit_execute_mutate returns False."""
    from workers.evidence_mutate_emit import emit_execute_mutate

    kafka = _ErrorKafkaCapture(fail_topic="omni-audit-chain")
    ctx = _make_ctx(kafka=kafka)

    # write_audit_block internally calls kafka for omni-audit-chain — that will fail
    # The AuditLedgerError wrapping causes fail-closed
    with patch(
        "workers.evidence_mutate_emit.write_audit_block",
        side_effect=AuditLedgerError("kafka send to omni-audit-chain failed"),
    ):
        result = await emit_execute_mutate(
            ctx,
            trace="chaos-kafka-002",
            tool_name="k8s_patch_configmap",
            args={"namespace": "multi-agent", "name": "test-cm", "data": {}},
        )

    assert result is False
    # No omni-actions messages since CRAT write failed before we got there
    assert not any(t == "omni-actions" for t, _ in kafka.sent)


async def test_kafka_missing_ctx_aborts_gracefully() -> None:
    """Missing kafka in context → emit_execute_mutate returns False without exception."""
    from workers.evidence_mutate_emit import emit_execute_mutate

    ctx = SimpleNamespace(
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        kafka=None,  # missing kafka
        settings=SimpleNamespace(
            kafka_topic_actions="omni-actions",
            kafka_topic_audit_chain="omni-audit-chain",
        ),
    )

    result = await emit_execute_mutate(
        ctx,
        trace="chaos-kafka-003",
        tool_name="k8s_rollout_restart",
        args={"namespace": "multi-agent", "deployment": "nginx-lab"},
    )

    assert result is False


async def test_kafka_missing_settings_aborts_gracefully() -> None:
    """Missing settings in context → emit_execute_mutate returns False without exception."""
    from workers.evidence_mutate_emit import emit_execute_mutate

    ctx = SimpleNamespace(
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        kafka=_KafkaCapture(),
        settings=None,  # missing settings
    )

    result = await emit_execute_mutate(
        ctx,
        trace="chaos-kafka-004",
        tool_name="k8s_rollout_restart",
        args={"namespace": "multi-agent", "deployment": "nginx-lab"},
    )

    assert result is False
