"""CRAT fail-closed coverage for the RemoteAgent diagnosis lane.

P0 fix: `_run_diagnosis_and_notify` previously emitted Telegram after storing
the diagnosis session but never wrote a CRAT audit block — violating the
AGENTS.md invariant "write_audit_block() MUST succeed trước Telegram emit /
action dispatch", which applies to every lane, not only K8s/advisory.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fakeredis.aioredis import FakeRedis

from services.audit_ledger.signer import AuditLedgerError
from workers.remote_agent_pipeline import _run_diagnosis_and_notify


def _ctx(chat_id: int = 12345) -> SimpleNamespace:
    redis = FakeRedis(decode_responses=True)
    settings = SimpleNamespace(kafka_topic_audit_chain="omni-audit-chain")
    return SimpleNamespace(
        redis=redis,
        kafka=None,
        settings=settings,
        telegram=AsyncMock(),
        telegram_chat_id=chat_id,
    )


def _session() -> dict:
    return {
        "trace_id": "trace-crat-1",
        "agent_id": "agent-1",
        "total_turns": 2,
        "degraded": False,
        "final": {
            "root_cause": "disk 96% on /var",
            "confidence": 0.8,
            "affected_components": ["disk"],
        },
    }


@pytest.mark.asyncio
async def test_crat_block_written_before_telegram_emit():
    """Audit block must be written, and BEFORE the Telegram send is invoked."""
    ctx = _ctx()
    ev_doc = {"probe": "remote_system_metrics", "lane": "SYS_RESOURCE", "tenant_id": "acme"}
    call_order: list[str] = []

    async def _fake_write_audit_block(**kwargs):
        call_order.append("crat")
        return {"seq": 1, "block_hash": "x"}

    async def _fake_emit(*args, **kwargs):
        call_order.append("telegram")

    with (
        patch(
            "services.analyst.diagnosis_loop.run_diagnosis_loop",
            new=AsyncMock(return_value=_session()),
        ),
        patch("workers.remote_agent_pipeline.write_audit_block", new=_fake_write_audit_block),
        patch("workers.remote_agent_pipeline.emit_diagnosis_to_telegram", new=_fake_emit),
    ):
        await _run_diagnosis_and_notify(
            ctx, ev_doc, "agent-1", "trace-crat-1", llm=AsyncMock(), model="qwen2.5-coder:7b",
            num_ctx=8192, chat_id=12345,
        )

    assert call_order == ["crat", "telegram"], (
        f"expected CRAT write before Telegram emit, got order={call_order}"
    )

    stages = await ctx.redis.hgetall("omni:trace:stages:trace-crat-1")
    assert json.loads(stages["CRAT"])["status"] == "ok"
    assert json.loads(stages["DISPATCH"])["status"] == "ok"


@pytest.mark.asyncio
async def test_telegram_not_emitted_when_crat_write_fails():
    """Fail-closed: if the audit block write fails, Telegram must NOT fire."""
    ctx = _ctx()
    ev_doc = {"probe": "remote_system_metrics", "lane": "SYS_RESOURCE", "tenant_id": "acme"}

    async def _failing_write_audit_block(**kwargs):
        raise AuditLedgerError("simulated audit chain outage")

    emit_mock = AsyncMock()

    with (
        patch(
            "services.analyst.diagnosis_loop.run_diagnosis_loop",
            new=AsyncMock(return_value=_session()),
        ),
        patch("workers.remote_agent_pipeline.write_audit_block", new=_failing_write_audit_block),
        patch("workers.remote_agent_pipeline.emit_diagnosis_to_telegram", new=emit_mock),
    ):
        await _run_diagnosis_and_notify(
            ctx, ev_doc, "agent-1", "trace-crat-2", llm=AsyncMock(), model="qwen2.5-coder:7b",
            num_ctx=8192, chat_id=12345,
        )

    emit_mock.assert_not_called()
    stages = await ctx.redis.hgetall("omni:trace:stages:trace-crat-2")
    assert json.loads(stages["CRAT"])["status"] == "fail"
    assert "DISPATCH" not in stages


@pytest.mark.asyncio
async def test_audit_payload_carries_tenant_id_from_evidence():
    """tenant_id must flow from ev_doc into write_audit_block for chain isolation
    (audit_chain:{tenant_id}:* keys — see chain_writer._tenant_keys)."""
    ctx = _ctx()
    ev_doc = {"probe": "remote_system_metrics", "lane": "SYS_RESOURCE", "tenant_id": "acme-corp"}
    captured: dict = {}

    async def _capture_write_audit_block(**kwargs):
        captured.update(kwargs)
        return {"seq": 1, "block_hash": "x"}

    with (
        patch(
            "services.analyst.diagnosis_loop.run_diagnosis_loop",
            new=AsyncMock(return_value=_session()),
        ),
        patch("workers.remote_agent_pipeline.write_audit_block", new=_capture_write_audit_block),
        patch("workers.remote_agent_pipeline.emit_diagnosis_to_telegram", new=AsyncMock()),
    ):
        await _run_diagnosis_and_notify(
            ctx, ev_doc, "agent-1", "trace-crat-3", llm=AsyncMock(), model="qwen2.5-coder:7b",
            num_ctx=8192, chat_id=12345,
        )

    assert captured.get("tenant_id") == "acme-corp"
    assert captured.get("event_type") == "ADVISORY_DECISION"
    assert captured.get("trace_id") == "trace-crat-3"
