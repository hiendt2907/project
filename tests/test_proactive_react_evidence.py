"""Proactive ReAct: tool output must pass prepare_tool_return_for_llm (registered tools too)."""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from workers.proactive_observer import AnomalyEvent, _proactive_event_pipeline
from workers.tools import ToolCallPayload


@pytest.mark.asyncio
async def test_proactive_react_prepares_registered_tool_output(monkeypatch: pytest.MonkeyPatch) -> None:
    import workers.proactive_observer as po

    monkeypatch.setattr(po, "child_span", lambda *_a, **_k: contextlib.nullcontext())
    monkeypatch.setattr(po, "resolve_remediation_from_memory", AsyncMock(return_value=(False, "", {})))
    monkeypatch.setattr(po, "_resolve_from_action_experience", AsyncMock(return_value=(False, "", "", {})))
    monkeypatch.setattr(po, "_learning_governance_decision", AsyncMock(return_value=("allow", 1.0)))
    monkeypatch.setattr(po, "_update_learning_pattern_stats", AsyncMock())

    huge = "[STATUS] business_hit\nok\n" + ("x" * 12_000)

    async def fake_redis_health(_ctx: object, _args: object) -> str:
        return huge

    monkeypatch.setitem(po.TOOL_REGISTRY, "redis_health", fake_redis_health)

    parse_calls = {"n": 0}

    async def fake_parse(_ctx: object, _prompt: str) -> tuple[ToolCallPayload | None, float, str]:
        parse_calls["n"] += 1
        if parse_calls["n"] == 1:
            return (ToolCallPayload(tool="redis_health", args={}), 0.99, "ok")
        return (None, 0.0, "stop")

    monkeypatch.setattr(po, "_parse_fallback_tool_call", fake_parse)

    import workers.proactive_react_runner as react_runner

    prep_inputs: list[str] = []
    real_prep = react_runner.prepare_tool_return_for_llm

    def spy_prep(ctx: object, raw: str, *, max_chars: int | None = None) -> str:
        prep_inputs.append(str(raw))
        return real_prep(ctx, raw, max_chars=max_chars)

    monkeypatch.setattr(react_runner, "prepare_tool_return_for_llm", spy_prep)

    ctx = MagicMock()
    ws = ctx.settings
    ws.proactive_sop_collection = "c"
    ws.proactive_sop_score_threshold = 0.9
    ws.proactive_fallback_enabled = True
    ws.proactive_fallback_allow_tools = "redis_health"
    ws.proactive_fallback_bypass_policy_in_god_mode = False
    ws.proactive_fallback_confidence_min = 0.5
    ws.proactive_react_max_turns = 4
    ws.proactive_verify_keywords_fail = "error,failed"
    ws.proactive_tool_timeout_sec = 30.0
    ws.tool_output_max_chars = 1500
    ws.proactive_react_tool_output_max_chars = None
    ws.proactive_react_memory_max_chars = 3200
    ws.proactive_llm_prompt_max_chars = 4096
    ws.proactive_react_memory_line_max_chars = 2000
    ws.proactive_resource_freeze_enabled = False
    ws.proactive_freeze_key_prefix = "omni:proactive:freeze:res"
    ws.proactive_freeze_namespace_fallback_allowed = False
    ws.audit_proactive_stream = "audit:proactive"
    ws.audit_proactive_maxlen = 1000
    ws.telegram_admin_chat_id = None
    ctx.telegram = None
    ctx.redis = AsyncMock()
    ctx.redis.rpush = AsyncMock()
    ctx.redis.expire = AsyncMock()
    ctx.redis.xadd = AsyncMock()

    ev = AnomalyEvent(
        trace_id="trace-ev-1",
        rule_name="R1",
        canonical_query="cpu high",
        metric_value=9.0,
        threshold=1.0,
    )
    await _proactive_event_pipeline(ctx, ev, "m1", "pk1", "{}")

    assert len(prep_inputs) >= 1
    assert len(prep_inputs[0]) > 10_000


@pytest.mark.asyncio
async def test_proactive_react_custom_output_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    import workers.proactive_observer as po

    monkeypatch.setattr(po, "child_span", lambda *_a, **_k: contextlib.nullcontext())
    monkeypatch.setattr(po, "resolve_remediation_from_memory", AsyncMock(return_value=(False, "", {})))
    monkeypatch.setattr(po, "_resolve_from_action_experience", AsyncMock(return_value=(False, "", "", {})))
    monkeypatch.setattr(po, "_learning_governance_decision", AsyncMock(return_value=("allow", 1.0)))
    monkeypatch.setattr(po, "_update_learning_pattern_stats", AsyncMock())

    body = "[STATUS] business_hit\n" + ("y" * 8000)

    async def fake_tool(_ctx: object, _args: object) -> str:
        return body

    monkeypatch.setitem(po.TOOL_REGISTRY, "redis_health", fake_tool)

    async def fake_parse(_ctx: object, _prompt: str) -> tuple[ToolCallPayload | None, float, str]:
        return (ToolCallPayload(tool="redis_health", args={}), 0.99, "ok")

    monkeypatch.setattr(po, "_parse_fallback_tool_call", fake_parse)

    ctx = MagicMock()
    ws = ctx.settings
    ws.proactive_sop_collection = "c"
    ws.proactive_sop_score_threshold = 0.9
    ws.proactive_fallback_enabled = True
    ws.proactive_fallback_allow_tools = "redis_health"
    ws.proactive_fallback_bypass_policy_in_god_mode = False
    ws.proactive_fallback_confidence_min = 0.5
    ws.proactive_react_max_turns = 2
    ws.proactive_verify_keywords_fail = "error,failed"
    ws.proactive_tool_timeout_sec = 30.0
    ws.tool_output_max_chars = 8000
    ws.proactive_react_tool_output_max_chars = 600
    ws.proactive_react_memory_max_chars = 3200
    ws.proactive_llm_prompt_max_chars = 4096
    ws.proactive_react_memory_line_max_chars = 2000
    ws.proactive_resource_freeze_enabled = False
    ws.proactive_freeze_key_prefix = "omni:proactive:freeze:res"
    ws.proactive_freeze_namespace_fallback_allowed = False
    ws.audit_proactive_stream = "audit:proactive"
    ws.audit_proactive_maxlen = 1000
    ws.telegram_admin_chat_id = None
    ctx.telegram = None
    ctx.redis = AsyncMock()
    ctx.redis.rpush = AsyncMock()
    ctx.redis.expire = AsyncMock()
    ctx.redis.xadd = AsyncMock()

    ev = AnomalyEvent(
        trace_id="trace-ev-2",
        rule_name="R1",
        canonical_query="cpu high",
        metric_value=9.0,
        threshold=1.0,
    )
    audit_rows: list[dict[str, object]] = []

    async def cap_audit(_ctx: object, **kwargs: object) -> None:
        audit_rows.append(dict(kwargs))

    monkeypatch.setattr(po, "_append_audit", cap_audit)

    await _proactive_event_pipeline(ctx, ev, "m2", "pk2", "{}")

    react = [a for a in audit_rows if str(a.get("outcome", "")).startswith("REACT_ITERATION")]
    assert react, audit_rows
    detail = str(react[0].get("detail") or "")
    assert len(detail) <= 650


@pytest.mark.asyncio
async def test_phase_policy_deny_increments_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import workers.proactive_observer as po

    monkeypatch.setattr(po, "child_span", lambda *_a, **_k: contextlib.nullcontext())
    monkeypatch.setattr(po, "resolve_remediation_from_memory", AsyncMock(return_value=(False, "", {})))
    monkeypatch.setattr(po, "_resolve_from_action_experience", AsyncMock(return_value=(False, "", "", {})))
    monkeypatch.setattr(po, "_learning_governance_decision", AsyncMock(return_value=("allow", 1.0)))
    monkeypatch.setattr(po, "_update_learning_pattern_stats", AsyncMock())

    async def fake_redis_health(_ctx: object, _args: object) -> str:
        return "[STATUS] business_hit\nok"

    monkeypatch.setitem(po.TOOL_REGISTRY, "redis_health", fake_redis_health)

    calls = {"n": 0}

    async def fake_parse(_ctx: object, prompt: str) -> tuple[ToolCallPayload | None, float, str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return (ToolCallPayload(tool="redis_health", args={}), 0.99, "ok")
        if calls["n"] == 2:
            # prescribe phase: redis_health is not in mutate allowlist → policy_deny
            return (ToolCallPayload(tool="redis_health", args={}), 0.99, "bad_phase")
        return (None, 0.0, "stop")

    monkeypatch.setattr(po, "_parse_fallback_tool_call", fake_parse)

    fb_reasons: list[str] = []

    def capture_fb(reason: str) -> None:
        fb_reasons.append(reason)

    monkeypatch.setattr("workers.proactive_react_runner.inc_proactive_fallback", capture_fb)

    ctx = MagicMock()
    ws = ctx.settings
    ws.proactive_sop_collection = "c"
    ws.proactive_sop_score_threshold = 0.9
    ws.proactive_fallback_enabled = True
    ws.proactive_fallback_allow_tools = "redis_health,k8s_rollout_restart"
    ws.proactive_fallback_bypass_policy_in_god_mode = False
    ws.proactive_fallback_confidence_min = 0.5
    ws.proactive_react_max_turns = 6
    ws.proactive_verify_keywords_fail = "error,failed"
    ws.proactive_tool_timeout_sec = 30.0
    ws.tool_output_max_chars = 1500
    ws.proactive_react_tool_output_max_chars = None
    ws.proactive_react_memory_max_chars = 3200
    ws.proactive_llm_prompt_max_chars = 4096
    ws.proactive_react_memory_line_max_chars = 2000
    ws.proactive_resource_freeze_enabled = False
    ws.proactive_freeze_key_prefix = "omni:proactive:freeze:res"
    ws.proactive_freeze_namespace_fallback_allowed = False
    ws.audit_proactive_stream = "audit:proactive"
    ws.audit_proactive_maxlen = 1000
    ws.telegram_admin_chat_id = None
    ctx.telegram = None
    ctx.redis = AsyncMock()
    ctx.redis.rpush = AsyncMock()
    ctx.redis.expire = AsyncMock()
    ctx.redis.xadd = AsyncMock()

    ev = AnomalyEvent(
        trace_id="trace-pd-1",
        rule_name="R1",
        canonical_query="cpu high",
        metric_value=9.0,
        threshold=1.0,
    )
    await _proactive_event_pipeline(ctx, ev, "m3", "pk3", "{}")

    assert "policy_deny" in fb_reasons
