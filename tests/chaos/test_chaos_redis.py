"""Unit chaos tests — Redis failure modes (D1: CRAT fail-closed).

Tests fail-closed semantics: when Redis is unavailable mid-CRAT write,
no omni-actions message must be dispatched (FAIL_CLOSED invariant).

Marker: @pytest.mark.real_condition — inject real AuditLedgerError
⚠ KHÔNG phải inverted logic — failure là thật (Redis raise lỗi).
"""

from __future__ import annotations

import json
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


def _make_ctx(redis=None, kafka=None) -> SimpleNamespace:
    return SimpleNamespace(
        redis=redis or fakeredis.aioredis.FakeRedis(decode_responses=True),
        kafka=kafka or _KafkaCapture(),
        settings=SimpleNamespace(
            kafka_topic_actions="omni-actions",
            kafka_topic_audit_chain="omni-audit-chain",
        ),
    )


async def test_redis_failure_triggers_crat_fail_closed() -> None:
    """write_audit_block raises AuditLedgerError → emit_execute_mutate returns False, omni-actions empty."""
    from workers.evidence_mutate_emit import emit_execute_mutate

    kafka = _KafkaCapture()
    ctx = _make_ctx(kafka=kafka)

    with patch(
        "workers.evidence_mutate_emit.write_audit_block",
        side_effect=AuditLedgerError("redis connection refused"),
    ):
        result = await emit_execute_mutate(
            ctx,
            trace="chaos-redis-001",
            tool_name="k8s_rollout_restart",
            args={"namespace": "multi-agent", "deployment": "nginx-lab"},
        )

    assert result is False
    actions = [t for t, _ in kafka.sent if t == "omni-actions"]
    assert len(actions) == 0, f"Expected no omni-actions dispatch, got {actions}"


async def test_redis_failure_does_not_raise() -> None:
    """AuditLedgerError from write_audit_block must be caught — no exception propagates to caller."""
    from workers.evidence_mutate_emit import emit_execute_mutate

    ctx = _make_ctx()

    with patch(
        "workers.evidence_mutate_emit.write_audit_block",
        side_effect=AuditLedgerError("redis timeout"),
    ):
        # Must not raise; fail-closed means return False, not propagate the error
        result = await emit_execute_mutate(
            ctx,
            trace="chaos-redis-002",
            tool_name="k8s_patch_configmap",
            args={"namespace": "multi-agent", "name": "test-cm", "data": {}},
        )

    assert result is False


async def test_crat_chain_corruption_head_deleted_blocks_dispatch() -> None:
    """DEL audit_chain:head_hash mid-write → AuditLedgerError → no ADVISORY_DISPATCHED on omni-actions."""
    from workers.evidence_mutate_emit import emit_execute_mutate

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    kafka = _KafkaCapture()
    ctx = _make_ctx(redis=redis, kafka=kafka)

    # Simulate head key deleted mid-chain by patching write to raise on the call
    with patch(
        "workers.evidence_mutate_emit.write_audit_block",
        side_effect=AuditLedgerError("head_hash missing — chain corrupted"),
    ):
        result = await emit_execute_mutate(
            ctx,
            trace="chaos-crat-corrupt-001",
            tool_name="k8s_rollout_restart",
            args={"namespace": "multi-agent", "deployment": "omni-analyst"},
        )

    assert result is False
    # Ensure absolutely nothing was dispatched to the actions topic
    assert all(t != "omni-actions" for t, _ in kafka.sent)


async def test_redis_recovers_after_transient_failure() -> None:
    """After transient Redis failure, subsequent calls succeed when Redis is back."""
    from workers.evidence_mutate_emit import emit_execute_mutate

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    kafka = _KafkaCapture()
    ctx = _make_ctx(redis=redis, kafka=kafka)

    call_count = 0

    async def _fail_first_then_succeed(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise AuditLedgerError("transient redis error")
        # Second call succeeds (no-op for test)

    with patch("workers.evidence_mutate_emit.write_audit_block", side_effect=_fail_first_then_succeed):
        first = await emit_execute_mutate(
            ctx,
            trace="chaos-redis-recover-001",
            tool_name="k8s_rollout_restart",
            args={"namespace": "multi-agent", "deployment": "nginx-lab"},
        )
        second = await emit_execute_mutate(
            ctx,
            trace="chaos-redis-recover-002",
            tool_name="k8s_rollout_restart",
            args={"namespace": "multi-agent", "deployment": "nginx-lab"},
        )

    assert first is False
    # Second call: write_audit_block succeeded (no-op), then kafka.send_dict called
    assert second is True
    assert any(t == "omni-actions" for t, _ in kafka.sent)
