"""Phase 4 (0-6 roadmap): wiring dispatch_if_eligible into the diagnosis
pipeline's _run_diagnosis_and_notify — the last hop of the closed loop.

Auto-recovery dispatch runs AFTER CRAT + Telegram succeed (best-effort on top
of an already-recorded diagnosis) and must never itself break diagnosis
reporting, even if the dispatch call raises.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fakeredis.aioredis import FakeRedis

from workers.remote_agent_pipeline import _run_diagnosis_and_notify


def _ctx(**settings_kwargs) -> SimpleNamespace:
    redis = FakeRedis(decode_responses=True)
    defaults = {
        "kafka_topic_audit_chain": "omni-audit-chain",
        "omni_gateway_api_key": "",
        "omni_gateway_internal_url": "http://omni-gateway.multi-agent.svc.cluster.local:80",
    }
    settings = SimpleNamespace(**{**defaults, **settings_kwargs})
    return SimpleNamespace(
        redis=redis, kafka=None, settings=settings,
        telegram=AsyncMock(), telegram_chat_id=12345,
    )


def _session(suggested_recovery=None, confidence=0.9) -> dict:
    return {
        "trace_id": "trace-ar-1", "agent_id": "agent-1", "total_turns": 2, "degraded": False,
        "final": {
            "root_cause": "payment-api.service is inactive", "confidence": confidence,
            "affected_components": ["payment-api"], "suggested_recovery": suggested_recovery,
        },
    }


async def _run(ctx, ev_doc, session, trace="trace-ar-1"):
    with (
        patch("services.analyst.diagnosis_loop.run_diagnosis_loop", new=AsyncMock(return_value=session)),
        patch("workers.remote_agent_pipeline.write_audit_block",
              new=AsyncMock(return_value={"seq": 1, "block_hash": "x"})),
        patch("workers.remote_agent_pipeline.emit_diagnosis_to_telegram", new=AsyncMock()),
    ):
        await _run_diagnosis_and_notify(
            ctx, ev_doc, "agent-1", trace, llm=AsyncMock(), model="qwen2.5-coder:7b",
            num_ctx=8192, chat_id=12345,
        )


class TestAutoRecoveryDispatchWiring:
    async def test_no_suggested_recovery_skips_silently_no_stage_row(self):
        ctx = _ctx()
        ev_doc = {"probe": "x", "lane": "SYS_HARD_FAIL", "tenant_id": "acme"}
        await _run(ctx, ev_doc, _session(suggested_recovery=None))
        stages = await ctx.redis.hgetall("omni:trace:stages:trace-ar-1")
        assert "AUTO_RECOVERY" not in stages

    async def test_eligible_but_no_gateway_key_skips_silently(self):
        """omni_gateway_api_key unset is the deployed default (fail-closed) —
        must not be reported as a failure stage, it's expected-off."""
        ctx = _ctx(omni_gateway_api_key="")
        ev_doc = {"probe": "x", "lane": "SYS_HARD_FAIL", "tenant_id": "acme"}
        suggested = {"capability": "systemd.restart_unit", "unit": "payment-api.service"}
        await _run(ctx, ev_doc, _session(suggested_recovery=suggested))
        stages = await ctx.redis.hgetall("omni:trace:stages:trace-ar-1")
        assert "AUTO_RECOVERY" not in stages

    async def test_eligible_dispatch_records_ok_stage(self):
        ctx = _ctx(omni_gateway_api_key="test-key")
        ev_doc = {"probe": "x", "lane": "SYS_HARD_FAIL", "tenant_id": "acme"}
        suggested = {"capability": "systemd.restart_unit", "unit": "payment-api.service"}

        fake_result = {"dispatched": True, "reason": "dispatched",
                       "command_id": "cmd-1", "state": "QUEUED"}
        with patch("workers.auto_recovery_bridge.dispatch_if_eligible",
                   new=AsyncMock(return_value=fake_result)):
            await _run(ctx, ev_doc, _session(suggested_recovery=suggested))

        stages = await ctx.redis.hgetall("omni:trace:stages:trace-ar-1")
        row = json.loads(stages["AUTO_RECOVERY"])
        assert row["status"] == "ok"
        assert "cmd-1" in row["detail"]

    async def test_gateway_rejection_records_fail_stage_but_does_not_raise(self):
        ctx = _ctx(omni_gateway_api_key="test-key")
        ev_doc = {"probe": "x", "lane": "SYS_HARD_FAIL", "tenant_id": "acme"}
        suggested = {"capability": "systemd.restart_unit", "unit": "payment-api.service"}

        fake_result = {"dispatched": False, "reason": "dispatched", "command_id": "cmd-1",
                       "state": None, "http_status": 423,
                       "gateway_detail": {"detail": {"reason": "tier_gate_suggest"}}}
        with patch("workers.auto_recovery_bridge.dispatch_if_eligible",
                   new=AsyncMock(return_value=fake_result)):
            await _run(ctx, ev_doc, _session(suggested_recovery=suggested))

        stages = await ctx.redis.hgetall("omni:trace:stages:trace-ar-1")
        assert json.loads(stages["AUTO_RECOVERY"])["status"] == "fail"

    async def test_dispatch_bridge_exception_does_not_break_pipeline(self):
        """Dispatch itself raising (network error etc) must be swallowed —
        the diagnosis already succeeded and was already reported; that must
        stand regardless of what happens on this best-effort last hop."""
        ctx = _ctx(omni_gateway_api_key="test-key")
        ev_doc = {"probe": "x", "lane": "SYS_HARD_FAIL", "tenant_id": "acme"}
        suggested = {"capability": "systemd.restart_unit", "unit": "payment-api.service"}

        with patch("workers.auto_recovery_bridge.dispatch_if_eligible",
                   new=AsyncMock(side_effect=RuntimeError("network down"))):
            await _run(ctx, ev_doc, _session(suggested_recovery=suggested))  # must not raise

        stages = await ctx.redis.hgetall("omni:trace:stages:trace-ar-1")
        assert json.loads(stages["CRAT"])["status"] == "ok"
        assert json.loads(stages["DISPATCH"])["status"] == "ok"
        assert json.loads(stages["AUTO_RECOVERY"])["status"] == "fail"
