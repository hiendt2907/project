"""CRAT ordering + Kafka enqueue behavior for emit_execute_mutate."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.audit_ledger.crat_event_types import CRAT_EVENT_MUTATION_ENQUEUED
from services.audit_ledger.signer import AuditLedgerError


@pytest.mark.asyncio
async def test_emit_execute_mutate_fail_closed_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    from workers import evidence_mutate_emit as mod

    async def boom_audit(**_: object) -> None:
        raise AuditLedgerError("simulated audit failure")

    monkeypatch.setattr(mod, "write_audit_block", boom_audit)

    ctx = MagicMock()
    ctx.kafka = AsyncMock()
    ctx.redis = MagicMock()
    ctx.settings.kafka_topic_actions = "omni-actions"
    ctx.settings.kafka_topic_audit_chain = "omni-audit-chain"

    ok = await mod.emit_execute_mutate(
        ctx,
        trace="tr-audit-fail",
        tool_name="k8s_rollout_restart",
        args={"namespace": "multi-agent", "deployment": "demo"},
        attempt_count=1,
    )
    assert ok is False
    ctx.kafka.send_dict.assert_not_called()


@pytest.mark.asyncio
async def test_emit_execute_mutate_kafka_failure_after_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    from workers import evidence_mutate_emit as mod

    audit_calls: list[str] = []

    async def audit_ok(**kwargs: object) -> dict:
        audit_calls.append(str(kwargs.get("event_type")))
        return {"seq": 1}

    monkeypatch.setattr(mod, "write_audit_block", audit_ok)

    ctx = MagicMock()
    ctx.kafka = AsyncMock()
    ctx.kafka.send_dict = AsyncMock(side_effect=RuntimeError("kafka unavailable"))
    ctx.redis = AsyncMock()
    ctx.redis.get = AsyncMock(return_value=None)
    ctx.settings.kafka_topic_actions = "omni-actions"
    ctx.settings.kafka_topic_audit_chain = "omni-audit-chain"

    ok = await mod.emit_execute_mutate(
        ctx,
        trace="tr-kafka-fail",
        tool_name="k8s_rollout_restart",
        args={"namespace": "multi-agent", "deployment": "demo"},
        attempt_count=2,
    )
    assert ok is False
    ctx.kafka.send_dict.assert_called_once()
    assert audit_calls and audit_calls[0] == CRAT_EVENT_MUTATION_ENQUEUED


@pytest.mark.asyncio
async def test_emit_execute_mutate_missing_redis_returns_false() -> None:
    from workers.evidence_mutate_emit import emit_execute_mutate

    ctx = MagicMock()
    ctx.kafka = AsyncMock()
    ctx.redis = None
    ctx.settings.kafka_topic_actions = "omni-actions"
    ctx.settings.kafka_topic_audit_chain = "omni-audit-chain"

    ok = await emit_execute_mutate(
        ctx,
        trace="tr-no-redis",
        tool_name="k8s_rollout_restart",
        args={"namespace": "x"},
        attempt_count=1,
    )
    assert ok is False
    ctx.kafka.send_dict.assert_not_called()

@pytest.mark.asyncio
async def test_emit_execute_mutate_blocked_when_kill_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kill-switch tại PRODUCER (2026-07-31): OMNI_AUTO_EXECUTE_ENABLED=false ⇒
    KHÔNG ghi omni-actions VÀ KHÔNG ghi CRAT MUTATION_ENQUEUED.

    Trước đây switch chỉ chặn ở consumer nên hệ vẫn sản xuất action + audit enqueue
    cho việc chưa được phép; mất offset + switch bật = replay 7 ngày mutate.
    """
    from workers import evidence_mutate_emit as mod

    audit_calls: list[str] = []

    async def spy_audit(**kwargs: object) -> dict:
        audit_calls.append(str(kwargs.get("event_type")))
        return {"seq": 1}

    monkeypatch.setattr(mod, "write_audit_block", spy_audit)

    ctx = MagicMock()
    ctx.kafka = AsyncMock()
    ctx.redis = AsyncMock()
    ctx.settings.kafka_topic_actions = "omni-actions"
    ctx.settings.kafka_topic_audit_chain = "omni-audit-chain"
    ctx.settings.omni_auto_execute_enabled = False  # tường minh: kill-switch TẮT

    ok = await mod.emit_execute_mutate(
        ctx,
        trace="tr-killswitch",
        tool_name="k8s_rollout_restart",
        args={"namespace": "multi-agent", "deployment": "demo"},
        attempt_count=1,
    )
    assert ok is False
    ctx.kafka.send_dict.assert_not_called()
    assert audit_calls == [], "khong duoc ghi CRAT MUTATION_ENQUEUED khi switch tat"
