"""Đ53 — known-fix reflex nối vào `remote_agent_pipeline.py` (nhánh service/
application, ~96% traffic thật per audit Đ51). Trước đây reflex chỉ sống ở
`knowledge_pipeline._try_known_fix_reflex` (nhánh metric-deviation, os_host) —
mọi sự cố critical/high đi qua `handle_remote_agent_evidence` luôn chạy full
vòng LLM 8 lượt dù `action_experience` đã có fix kiểm chứng cho đúng host đó.

Test bám hành vi quan sát được (mark_stage LLM=skip, không launch diagnosis
loop, giá trị trả về), không bám implementation detail của
`try_remote_known_fix`/`find_known_fix_candidate` (đã có test riêng).
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fakeredis.aioredis import FakeRedis

from remote_agent.discovery import save_discovery_snapshot
from workers.remote_agent_pipeline import handle_remote_agent_evidence

pytestmark = pytest.mark.asyncio


def _ctx() -> SimpleNamespace:
    redis = FakeRedis(decode_responses=True)
    settings = SimpleNamespace(
        action_experience_score_threshold=0.55,
        telegram_admin_chat_id=None,
    )
    return SimpleNamespace(
        redis=redis, kafka=None, settings=settings,
        telegram=AsyncMock(), telegram_chat_id=12345,
    )


def _ev_doc(**overrides) -> dict:
    base = {
        "probe": "remote_service_status",
        "domain": "service",
        "tenant_id": "acme",
        "namespace": "cust-app",
        "alert_hint": "payment-api unit inactive",
        "extracted_fact": {"agent_id": "agent-1", "result": "FAILED"},
    }
    base.update(overrides)
    return base


async def _seed_snapshot(ctx, tenant_id="acme", agent_id="agent-1"):
    await save_discovery_snapshot(
        ctx.redis, tenant_id=tenant_id, agent_id=agent_id,
        snapshot={"services": [{"name": "payment-api"}]},
    )


class TestKnownFixReflexDispatches:
    async def test_reflex_dispatch_skips_full_llm_loop(self):
        ctx = _ctx()
        await _seed_snapshot(ctx)
        fake_result = {"resolved": True, "dispatched": True,
                       "command_id": "cmd-reflex-1", "reason": "dispatched"}
        with (
            patch("workers.remote_known_fix.try_remote_known_fix",
                  new=AsyncMock(return_value=fake_result)),
            patch("services.analyst.diagnosis_loop.run_diagnosis_loop",
                  new=AsyncMock()) as diag_loop,
        ):
            result = await handle_remote_agent_evidence(ctx, _ev_doc(), "trace-reflex-1")

        assert "known_fix_reflex" in result
        diag_loop.assert_not_called()

        stages = await ctx.redis.hgetall("omni:trace:stages:trace-reflex-1")
        row = json.loads(stages["LLM"])
        assert row["status"] == "skip"
        assert "known_fix_reflex" in row["detail"]

    async def test_reflex_passes_host_scope_and_query_from_evidence(self):
        ctx = _ctx()
        await _seed_snapshot(ctx)
        fake_result = {"resolved": True, "dispatched": True, "command_id": "cmd-x"}
        spy = AsyncMock(return_value=fake_result)
        with (
            patch("workers.remote_known_fix.try_remote_known_fix", new=spy),
            patch("services.analyst.diagnosis_loop.run_diagnosis_loop", new=AsyncMock()),
        ):
            await handle_remote_agent_evidence(ctx, _ev_doc(), "trace-reflex-2")

        kwargs = spy.await_args.kwargs
        assert kwargs["agent_id"] == "agent-1"
        assert kwargs["tenant_id"] == "acme"
        assert "payment-api" in kwargs["host_scope"]
        assert "payment-api.service" in kwargs["host_scope"]
        assert "payment-api" in kwargs["query_text"] or "service" in kwargs["query_text"]


class TestKnownFixReflexFallsThrough:
    async def test_no_discovery_snapshot_falls_back_to_full_diagnosis(self):
        """Không có snapshot ⇒ chưa biết host chạy service gì ⇒ đi thẳng đường
        LLM đầy đủ như trước — an toàn hơn liều thực thi trên host chưa biết."""
        ctx = _ctx()
        # Cố ý KHÔNG seed snapshot.
        fake_session = {
            "trace_id": "trace-reflex-3", "agent_id": "agent-1", "total_turns": 2,
            "degraded": False,
            "final": {"root_cause": "x", "confidence": 0.9,
                      "affected_components": [], "suggested_recovery": None},
        }
        with (
            patch("services.analyst.diagnosis_loop.run_diagnosis_loop",
                  new=AsyncMock(return_value=fake_session)) as diag_loop,
            patch("workers.remote_agent_pipeline.write_audit_block",
                  new=AsyncMock(return_value={"seq": 1, "block_hash": "x"})),
            patch("workers.remote_agent_pipeline.emit_diagnosis_to_telegram", new=AsyncMock()),
            patch("workers.remote_known_fix.try_remote_known_fix") as reflex_spy,
        ):
            result = await handle_remote_agent_evidence(ctx, _ev_doc(), "trace-reflex-3")
            import asyncio
            await asyncio.sleep(0)  # let the background diagnosis task start

        reflex_spy.assert_not_called()
        assert "known_fix_reflex" not in result

    async def test_reflex_no_candidate_falls_back_to_full_diagnosis(self):
        ctx = _ctx()
        await _seed_snapshot(ctx)
        fake_session = {
            "trace_id": "trace-reflex-4", "agent_id": "agent-1", "total_turns": 2,
            "degraded": False,
            "final": {"root_cause": "x", "confidence": 0.9,
                      "affected_components": [], "suggested_recovery": None},
        }
        with (
            patch("workers.remote_known_fix.try_remote_known_fix",
                  new=AsyncMock(return_value={"resolved": False, "reason": "no_candidate"})),
            patch("services.analyst.diagnosis_loop.run_diagnosis_loop",
                  new=AsyncMock(return_value=fake_session)) as diag_loop,
            patch("workers.remote_agent_pipeline.write_audit_block",
                  new=AsyncMock(return_value={"seq": 1, "block_hash": "x"})),
            patch("workers.remote_agent_pipeline.emit_diagnosis_to_telegram", new=AsyncMock()),
        ):
            result = await handle_remote_agent_evidence(ctx, _ev_doc(), "trace-reflex-4")
            import asyncio
            await asyncio.sleep(0)

        assert "known_fix_reflex" not in result
