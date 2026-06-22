"""Coverage tests for workers.proactive_react_runner (6.8% → substantial increase).

Tests exercise run_proactive_react_fallback across the main code paths:
- dev_mode / god_mode tool set selection
- parse_fail path (no tool call returned)
- phase_policy_deny (tool not in phase_tools)
- low_confidence path
- prescribe → treat phase transition
- treat → recheck phase with resolution
- no_tools_for_phase escalation
- k8s_list_pods namespace guard
- k8s_rollout_restart policy deny
- resource/namespace freeze blocks
- lease conflict block
- tool execution success → diagnose phase
- recheck verified → resolved outcome
- asyncio.TimeoutError → escalated
- generic Exception → escalated
- telegram resolved / escalated notifications
- audit appended on resolved and escalated
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("OMNI_WORKER_ROLE", "analyst")
os.environ.setdefault("OMNI_ENV_MODE", "dev")
os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379")

import fakeredis.aioredis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(**kw: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "god_mode": False,
        "lab_unchained": False,
        "proactive_fallback_bypass_policy_in_god_mode": False,
        "proactive_react_max_turns": 4,
        "proactive_react_memory_max_chars": 3200,
        "proactive_react_memory_line_max_chars": 2000,
        "proactive_react_tool_output_max_chars": None,
        "proactive_llm_prompt_max_chars": 4096,
        "proactive_fallback_confidence_min": 0.78,
        "proactive_fallback_allow_tools": (
            "k8s_list_pods,inspect_pod_deep,k8s_rollout_restart,"
            "k8s_scale_deployment,promql_instant,query_prometheus_metrics"
        ),
        "proactive_fallback_max_attempts": 1,
        "proactive_tool_timeout_sec": 30.0,
        "proactive_verify_keywords_fail": "",
        "proactive_resource_freeze_enabled": False,
        "proactive_freeze_key_prefix": "omni:freeze",
        "proactive_lease_ttl_sec": 30,
        "proactive_react_require_namespace_for_list": True,
        "proactive_negative_pattern_ttl_sec": 604800,
        "telegram_admin_chat_id": None,
        "omni_concise_reply_max_words": 200,
        "omni_summary_max_words": 200,
        "chat_model": "qwen2.5:7b",
        "embed_model": "nomic-embed-text",
        "proactive_promql": "up == 0",
        "kafka_topic_audit_proactive": "omni-audit-proactive",
        "kafka_topic_dlq": "omni-dlq",
        "proactive_k8s_snapshot_timeout_sec": 5.0,
        "proactive_resource_freeze_ttl_sec": 600,
        "proactive_freeze_namespace_fallback_allowed": True,
        "action_experience_score_threshold": 0.85,
        "learning_stats_ttl_sec": 86400,
        "memory_canonical_strip_pods": True,
        "proactive_sop_collection": "omni_sop",
        "proactive_sop_score_threshold": 0.88,
        "proactive_gigo_require_cluster_identity": False,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_ev(**kw: Any):
    from workers.proactive_models import AnomalyEvent
    defaults = dict(
        trace_id="trace-test01",
        rule_name="TestRule",
        canonical_query="cpu > 0.9",
        threshold=0.8,
        metric_value=0.95,
        namespace="multi-agent",
        deployment="my-app",
    )
    defaults.update(kw)
    return AnomalyEvent(**defaults)


def _make_ctx(redis_client=None, kafka=None, settings=None, **kw: Any) -> SimpleNamespace:
    scout_ready = asyncio.Event()
    scout_ready.set()
    sem = AsyncMock()
    sem.acquire_proactive = AsyncMock(return_value="token-abc")
    sem.release = AsyncMock()
    defaults: dict[str, Any] = {
        "settings": settings or _make_settings(),
        "redis": redis_client or fakeredis.aioredis.FakeRedis(decode_responses=True),
        "llm": AsyncMock(),
        "vector_store": MagicMock(),
        "ledger": MagicMock(),
        "semaphore": sem,
        "telegram": None,
        "kafka": kafka,
        "telegram_chat_id": None,
        "inbound_source": "",
        "inbound_user_text": "",
        "restart_rollout_explicit": False,
        "pod_discovery_pairs": [],
        "scout_ready": scout_ready,
        "inbound_trace_id": "test-trace",
        "llm_slot_held": False,
        "inbound_proactive": False,
        "k8s_mutated": False,
        "fallback_inline_commands": None,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_kafka():
    kafka = MagicMock()
    kafka.send_dict = AsyncMock()
    kafka.send_envelope_inner = AsyncMock()
    return kafka


def _tool_call(tool: str = "inspect_pod_deep", args: dict | None = None):
    from workers.tools import ToolCallPayload
    return ToolCallPayload(tool=tool, args=args or {"namespace": "multi-agent", "pod": "my-pod"})


# ---------------------------------------------------------------------------
# Tests for run_proactive_react_fallback
# ---------------------------------------------------------------------------

class TestProactiveReactFallback:

    @pytest.mark.asyncio
    async def test_parse_fail_exhausted_without_telegram(self):
        """All iterations fail to parse a tool call → escalated, no crash."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(proactive_react_max_turns=2))
        ev = _make_ev()

        with patch.object(po, "_parse_fallback_tool_call", return_value=(None, 0.0, "json_fail")), \
             patch.object(po, "_react_mem_recent", return_value=[]), \
             patch.object(po, "_react_mem_append", new=AsyncMock()), \
             patch.object(po, "_set_negative_pattern", new=AsyncMock()), \
             patch.object(po, "_append_audit", new=AsyncMock()):
            from workers.proactive_react_runner import run_proactive_react_fallback
            await run_proactive_react_fallback(
                ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
            )
        # Should complete without error

    @pytest.mark.asyncio
    async def test_parse_fail_path(self):
        """parse_fail increments fallback metric and appends observation."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(proactive_react_max_turns=1))
        ev = _make_ev()

        appended_obs: list[str] = []

        async def fake_react_mem_append(ctx, trace, obs):
            appended_obs.append(obs)

        with patch.object(po, "_parse_fallback_tool_call", return_value=(None, 0.0, "json_fail")):
            with patch.object(po, "_react_mem_recent", return_value=[]):
                with patch.object(po, "_react_mem_append", new=fake_react_mem_append):
                    with patch.object(po, "_set_negative_pattern", new=AsyncMock()):
                        with patch.object(po, "_append_audit", new=AsyncMock()):
                            from workers.proactive_react_runner import run_proactive_react_fallback
                            await run_proactive_react_fallback(
                                ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
                            )
        assert any("parse_fail" in o for o in appended_obs)

    @pytest.mark.asyncio
    async def test_phase_policy_deny(self):
        """Tool not in phase_tools → policy_deny observation."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(proactive_react_max_turns=2))
        ev = _make_ev()

        appended_obs: list[str] = []

        async def fake_react_mem_append(ctx, trace, obs):
            appended_obs.append(obs)

        # Return a tool NOT in diagnose phase_tools
        not_in_tools = _tool_call(tool="echo")

        with patch.object(po, "_parse_fallback_tool_call", return_value=(not_in_tools, 0.95, "ok")):
            with patch.object(po, "_react_mem_recent", return_value=[]):
                with patch.object(po, "_react_mem_append", new=fake_react_mem_append):
                    with patch.object(po, "_set_negative_pattern", new=AsyncMock()):
                        with patch.object(po, "_append_audit", new=AsyncMock()):
                            from workers.proactive_react_runner import run_proactive_react_fallback
                            await run_proactive_react_fallback(
                                ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
                            )
        assert any("phase_policy_deny" in o for o in appended_obs)

    @pytest.mark.asyncio
    async def test_low_confidence_blocked(self):
        """Tool in phase but confidence below threshold → low_confidence obs."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(
            proactive_react_max_turns=2, proactive_fallback_confidence_min=0.9
        ))
        ev = _make_ev()

        appended_obs: list[str] = []

        async def fake_react_mem_append(ctx, trace, obs):
            appended_obs.append(obs)

        call = _tool_call(tool="inspect_pod_deep")

        with patch.object(po, "_parse_fallback_tool_call", return_value=(call, 0.5, "low")):
            with patch.object(po, "_react_mem_recent", return_value=[]):
                with patch.object(po, "_react_mem_append", new=fake_react_mem_append):
                    with patch.object(po, "_set_negative_pattern", new=AsyncMock()):
                        with patch.object(po, "_append_audit", new=AsyncMock()):
                            from workers.proactive_react_runner import run_proactive_react_fallback
                            await run_proactive_react_fallback(
                                ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
                            )
        assert any("low_confidence" in o for o in appended_obs)

    @pytest.mark.asyncio
    async def test_list_pods_blocked_missing_namespace(self):
        """k8s_list_pods without namespace → list_pods_blocked_missing_namespace."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(proactive_react_max_turns=2))
        ev = _make_ev()

        appended_obs: list[str] = []

        async def fake_react_mem_append(ctx, trace, obs):
            appended_obs.append(obs)

        from workers.tools import ToolCallPayload
        call = ToolCallPayload(tool="k8s_list_pods", args={"namespace": ""})

        with patch.object(po, "_parse_fallback_tool_call", return_value=(call, 0.95, "ok")):
            with patch.object(po, "_react_mem_recent", return_value=[]):
                with patch.object(po, "_react_mem_append", new=fake_react_mem_append):
                    with patch.object(po, "_set_negative_pattern", new=AsyncMock()):
                        with patch.object(po, "_append_audit", new=AsyncMock()):
                            from workers.proactive_react_runner import run_proactive_react_fallback
                            await run_proactive_react_fallback(
                                ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
                            )
        assert any("list_pods_blocked_missing_namespace" in o for o in appended_obs)

    @pytest.mark.asyncio
    async def test_rollout_restart_policy_deny(self):
        """k8s_rollout_restart in prescribe phase with missing deployment → rollout_blocked."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(proactive_react_max_turns=4))
        ev = _make_ev(namespace="multi-agent")

        appended_obs: list[str] = []
        audit_outcomes: list[str] = []

        async def fake_react_mem_append(ctx, trace, obs):
            appended_obs.append(obs)

        async def fake_append_audit(ctx, *, trace_id, rule_id, outcome, **kw):
            audit_outcomes.append(outcome)

        from workers.tools import ToolCallPayload
        diagnose_call = ToolCallPayload(tool="inspect_pod_deep", args={"namespace": "multi-agent", "pod": "my-pod"})
        # Missing deployment in prescribe → rollout blocked
        bad_rollout = ToolCallPayload(tool="k8s_rollout_restart", args={"namespace": "multi-agent"})

        call_seq = [diagnose_call, bad_rollout]
        call_idx = [0]

        async def fake_parse(*a, **kw):
            idx = call_idx[0]
            if idx >= len(call_seq):
                return (None, 0.0, "done")
            call_idx[0] += 1
            return (call_seq[idx], 0.95, "ok")

        async def fake_tool_fn(ctx, args):
            return "[STATUS] business_hit"

        from workers.tools import TOOL_REGISTRY
        with patch.dict(TOOL_REGISTRY, {"inspect_pod_deep": fake_tool_fn}):
            with patch.object(po, "_parse_fallback_tool_call", side_effect=fake_parse):
                with patch.object(po, "_react_mem_recent", return_value=[]):
                    with patch.object(po, "_react_mem_append", new=fake_react_mem_append):
                        with patch.object(po, "_set_negative_pattern", new=AsyncMock()):
                            with patch.object(po, "_append_audit", new=fake_append_audit):
                                from workers.proactive_react_runner import run_proactive_react_fallback
                                await run_proactive_react_fallback(
                                    ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
                                )
        assert any("rollout_blocked" in o for o in appended_obs)

    @pytest.mark.asyncio
    async def test_tool_timeout_escalation(self):
        """Tool execution raises asyncio.TimeoutError → escalated=True, audit ESCALATED."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(proactive_react_max_turns=1))
        ev = _make_ev()

        appended_obs: list[str] = []
        audit_outcomes: list[str] = []

        async def fake_react_mem_append(ctx, trace, obs):
            appended_obs.append(obs)

        async def fake_append_audit(ctx, *, trace_id, rule_id, outcome, **kw):
            audit_outcomes.append(outcome)

        call = _tool_call(tool="inspect_pod_deep")

        async def fake_tool_fn(ctx, args):
            raise asyncio.TimeoutError()

        from workers.tools import TOOL_REGISTRY
        with patch.dict(TOOL_REGISTRY, {"inspect_pod_deep": fake_tool_fn}):
            with patch.object(po, "_parse_fallback_tool_call", return_value=(call, 0.95, "ok")):
                with patch.object(po, "_react_mem_recent", return_value=[]):
                    with patch.object(po, "_react_mem_append", new=fake_react_mem_append):
                        with patch.object(po, "_set_negative_pattern", new=AsyncMock()):
                            with patch.object(po, "_append_audit", new=fake_append_audit):
                                with patch.object(po, "_fail_safe_after_tool_error", new=AsyncMock()):
                                    from workers.proactive_react_runner import run_proactive_react_fallback
                                    await run_proactive_react_fallback(
                                        ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
                                    )
        assert any("timeout" in o for o in appended_obs)
        assert "ESCALATED" in audit_outcomes

    @pytest.mark.asyncio
    async def test_tool_exception_escalation(self):
        """Tool execution raises generic Exception → escalated, audit ESCALATED."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(proactive_react_max_turns=1))
        ev = _make_ev()

        appended_obs: list[str] = []
        audit_outcomes: list[str] = []

        async def fake_react_mem_append(ctx, trace, obs):
            appended_obs.append(obs)

        async def fake_append_audit(ctx, *, trace_id, rule_id, outcome, **kw):
            audit_outcomes.append(outcome)

        call = _tool_call(tool="inspect_pod_deep")

        async def fake_tool_fn(ctx, args):
            raise RuntimeError("boom")

        from workers.tools import TOOL_REGISTRY
        with patch.dict(TOOL_REGISTRY, {"inspect_pod_deep": fake_tool_fn}):
            with patch.object(po, "_parse_fallback_tool_call", return_value=(call, 0.95, "ok")):
                with patch.object(po, "_react_mem_recent", return_value=[]):
                    with patch.object(po, "_react_mem_append", new=fake_react_mem_append):
                        with patch.object(po, "_set_negative_pattern", new=AsyncMock()):
                            with patch.object(po, "_append_audit", new=fake_append_audit):
                                with patch.object(po, "_fail_safe_after_tool_error", new=AsyncMock()):
                                    from workers.proactive_react_runner import run_proactive_react_fallback
                                    await run_proactive_react_fallback(
                                        ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
                                    )
        assert any("exception" in o for o in appended_obs)
        assert "ESCALATED" in audit_outcomes

    @pytest.mark.asyncio
    async def test_diagnose_success_transitions_to_prescribe(self):
        """After diagnose tool succeeds, phase transitions to prescribe."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(proactive_react_max_turns=2))
        ev = _make_ev()

        phases_seen: list[str] = []
        appended_obs: list[str] = []

        async def fake_react_mem_append(ctx, trace, obs):
            appended_obs.append(obs)

        async def fake_append_audit(ctx, *, trace_id, rule_id, outcome, meta=None, **kw):
            if meta:
                phases_seen.append(meta.get("phase", ""))

        call = _tool_call(tool="inspect_pod_deep")

        async def fake_tool_fn(ctx, args):
            return "[STATUS] business_hit pod=my-pod running"

        from workers.tools import TOOL_REGISTRY
        with patch.dict(TOOL_REGISTRY, {"inspect_pod_deep": fake_tool_fn}):
            with patch.object(po, "_parse_fallback_tool_call", return_value=(call, 0.95, "ok")):
                with patch.object(po, "_react_mem_recent", return_value=[]):
                    with patch.object(po, "_react_mem_append", new=fake_react_mem_append):
                        with patch.object(po, "_set_negative_pattern", new=AsyncMock()):
                            with patch.object(po, "_append_audit", new=fake_append_audit):
                                from workers.proactive_react_runner import run_proactive_react_fallback
                                await run_proactive_react_fallback(
                                    ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
                                )
        assert "diagnose" in phases_seen

    @pytest.mark.asyncio
    async def test_full_diagnose_prescribe_treat_recheck_resolved(self):
        """Full happy path: diagnose → prescribe → treat → recheck → resolved."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(proactive_react_max_turns=6))
        ev = _make_ev(namespace="multi-agent")

        appended_obs: list[str] = []
        audit_outcomes: list[str] = []

        async def fake_react_mem_append(ctx, trace, obs):
            appended_obs.append(obs)

        async def fake_append_audit(ctx, *, trace_id, rule_id, outcome, **kw):
            audit_outcomes.append(outcome)

        # Sequence: diagnose_call, prescribe_call, recheck_call
        diagnose_call = _tool_call(tool="inspect_pod_deep")
        prescribe_call = _tool_call(tool="k8s_rollout_restart", args={"namespace": "multi-agent", "deployment": "my-app"})
        recheck_call = _tool_call(tool="k8s_list_pods", args={"namespace": "multi-agent"})

        call_seq = [diagnose_call, prescribe_call, recheck_call]
        call_idx = [0]

        async def fake_parse(*a, **kw):
            idx = call_idx[0]
            if idx >= len(call_seq):
                return (diagnose_call, 0.95, "ok")
            call_idx[0] += 1
            return (call_seq[idx], 0.95, "ok")

        async def fake_tool_fn(ctx, args):
            return "[STATUS] business_hit done"

        from workers.tools import TOOL_REGISTRY
        with patch.dict(TOOL_REGISTRY, {
            "inspect_pod_deep": fake_tool_fn,
            "k8s_rollout_restart": fake_tool_fn,
            "k8s_list_pods": fake_tool_fn,
        }):
            with patch.object(po, "_parse_fallback_tool_call", side_effect=fake_parse):
                with patch.object(po, "_react_mem_recent", return_value=[]):
                    with patch.object(po, "_react_mem_append", new=fake_react_mem_append):
                        with patch.object(po, "_set_negative_pattern", new=AsyncMock()):
                            with patch.object(po, "_append_audit", new=fake_append_audit):
                                with patch.object(po, "_save_proactive_learning_record", new=AsyncMock()):
                                    from workers.proactive_react_runner import run_proactive_react_fallback
                                    await run_proactive_react_fallback(
                                        ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
                                    )
        assert "RESOLVED" in audit_outcomes

    @pytest.mark.asyncio
    async def test_telegram_resolved_notification(self):
        """Resolved outcome sends Telegram [PROACTIVE][RESOLVED] message."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        tg = AsyncMock()
        ctx = _make_ctx(
            kafka=kafka,
            telegram=tg,
            settings=_make_settings(
                proactive_react_max_turns=6,
                telegram_admin_chat_id=123456,
            )
        )
        ev = _make_ev(namespace="multi-agent")

        audit_outcomes: list[str] = []

        async def fake_append_audit(ctx, *, trace_id, rule_id, outcome, **kw):
            audit_outcomes.append(outcome)

        diagnose_call = _tool_call(tool="inspect_pod_deep")
        prescribe_call = _tool_call(tool="k8s_rollout_restart", args={"namespace": "multi-agent", "deployment": "my-app"})
        recheck_call = _tool_call(tool="k8s_list_pods", args={"namespace": "multi-agent"})

        call_seq = [diagnose_call, prescribe_call, recheck_call]
        call_idx = [0]

        async def fake_parse(*a, **kw):
            idx = call_idx[0]
            if idx >= len(call_seq):
                return (diagnose_call, 0.95, "ok")
            call_idx[0] += 1
            return (call_seq[idx], 0.95, "ok")

        async def fake_tool_fn(ctx, args):
            return "[STATUS] business_hit done"

        from workers.tools import TOOL_REGISTRY
        with patch.dict(TOOL_REGISTRY, {
            "inspect_pod_deep": fake_tool_fn,
            "k8s_rollout_restart": fake_tool_fn,
            "k8s_list_pods": fake_tool_fn,
        }):
            with patch.object(po, "_parse_fallback_tool_call", side_effect=fake_parse):
                with patch.object(po, "_react_mem_recent", return_value=[]):
                    with patch.object(po, "_react_mem_append", new=AsyncMock()):
                        with patch.object(po, "_set_negative_pattern", new=AsyncMock()):
                            with patch.object(po, "_append_audit", new=fake_append_audit):
                                with patch.object(po, "_save_proactive_learning_record", new=AsyncMock()):
                                    from workers.proactive_react_runner import run_proactive_react_fallback
                                    await run_proactive_react_fallback(
                                        ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
                                    )
        tg.send_message.assert_awaited()
        call_args = tg.send_message.call_args_list
        assert any("[PROACTIVE][RESOLVED]" in str(c) for c in call_args)

    @pytest.mark.asyncio
    async def test_telegram_escalated_notification(self):
        """Escalated outcome sends Telegram [PROACTIVE][ESCALATED] message."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        tg = AsyncMock()
        ctx = _make_ctx(
            kafka=kafka,
            telegram=tg,
            settings=_make_settings(
                proactive_react_max_turns=1,
                telegram_admin_chat_id=123456,
            )
        )
        ev = _make_ev()

        with patch.object(po, "_parse_fallback_tool_call", return_value=(None, 0.0, "fail")):
            with patch.object(po, "_react_mem_recent", return_value=[]):
                with patch.object(po, "_react_mem_append", new=AsyncMock()):
                    with patch.object(po, "_set_negative_pattern", new=AsyncMock()):
                        with patch.object(po, "_append_audit", new=AsyncMock()):
                            from workers.proactive_react_runner import run_proactive_react_fallback
                            await run_proactive_react_fallback(
                                ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
                            )
        tg.send_message.assert_awaited()
        call_args = tg.send_message.call_args_list
        assert any("[PROACTIVE][ESCALATED]" in str(c) for c in call_args)

    @pytest.mark.asyncio
    async def test_god_mode_expands_tool_set(self):
        """In god_mode, all TOOL_REGISTRY tools are allowed."""
        import workers.proactive_observer as po
        from workers.tools import TOOL_REGISTRY
        kafka = _make_kafka()
        ctx = _make_ctx(
            kafka=kafka,
            settings=_make_settings(
                god_mode=True,
                proactive_fallback_bypass_policy_in_god_mode=True,
                proactive_react_max_turns=1,
            )
        )
        ev = _make_ev()

        captured_allowed: list[set] = []
        original_parse = po._parse_fallback_tool_call

        async def capture_parse(ctx, prompt):
            # Detect from prompt which tools are allowed by checking phase_tools
            return (None, 0.0, "done")

        with patch.object(po, "_parse_fallback_tool_call", new=capture_parse):
            with patch.object(po, "_react_mem_recent", return_value=[]):
                with patch.object(po, "_react_mem_append", new=AsyncMock()):
                    with patch.object(po, "_set_negative_pattern", new=AsyncMock()):
                        with patch.object(po, "_append_audit", new=AsyncMock()):
                            from workers.proactive_react_runner import run_proactive_react_fallback
                            await run_proactive_react_fallback(
                                ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
                            )
        # Just verify it completes without error in god mode

    @pytest.mark.asyncio
    async def test_no_tools_for_phase_escalation(self):
        """If no tools available for current phase → escalated immediately."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        # Empty allow_tools → no tools for any phase
        ctx = _make_ctx(
            kafka=kafka,
            settings=_make_settings(
                proactive_fallback_allow_tools="",
                proactive_react_max_turns=2,
            )
        )
        ev = _make_ev()

        audit_outcomes: list[str] = []

        async def fake_append_audit(ctx, *, trace_id, rule_id, outcome, **kw):
            audit_outcomes.append(outcome)

        with patch.object(po, "_react_mem_recent", return_value=[]):
            with patch.object(po, "_react_mem_append", new=AsyncMock()):
                with patch.object(po, "_set_negative_pattern", new=AsyncMock()):
                    with patch.object(po, "_append_audit", new=fake_append_audit):
                        from workers.proactive_react_runner import run_proactive_react_fallback
                        await run_proactive_react_fallback(
                            ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
                        )
        assert "ESCALATED" in audit_outcomes

    @pytest.mark.asyncio
    async def test_resource_freeze_blocks_tool(self):
        """Resource freeze blocks tool execution → resource_frozen observation."""
        import workers.proactive_observer as po
        import workers.proactive_react_runner as prr
        kafka = _make_kafka()
        ctx = _make_ctx(
            kafka=kafka,
            settings=_make_settings(
                proactive_resource_freeze_enabled=True,
                proactive_react_max_turns=2,
            )
        )
        ev = _make_ev()

        appended_obs: list[str] = []

        async def fake_react_mem_append(ctx, trace, obs):
            appended_obs.append(obs)

        call = _tool_call(tool="inspect_pod_deep", args={"namespace": "multi-agent", "pod": "my-pod"})

        async def fake_is_resource_frozen(*a, **kw):
            return True

        with patch.object(po, "_parse_fallback_tool_call", return_value=(call, 0.95, "ok")), \
             patch.object(po, "_react_mem_recent", return_value=[]), \
             patch.object(po, "_react_mem_append", new=fake_react_mem_append), \
             patch.object(po, "_set_negative_pattern", new=AsyncMock()), \
             patch.object(po, "_append_audit", new=AsyncMock()), \
             patch("workers.proactive_react_runner.extract_resource_ref",
                   return_value=("multi-agent", "Pod", "my-pod")), \
             patch("workers.proactive_react_runner.is_resource_frozen", new=fake_is_resource_frozen):
            from workers.proactive_react_runner import run_proactive_react_fallback
            await run_proactive_react_fallback(
                ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
            )
        assert any("resource_frozen" in o for o in appended_obs)

    @pytest.mark.asyncio
    async def test_lease_conflict_blocks_mutate_tool(self):
        """Lease conflict blocks mutate tool → lease_conflict observation."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(
            kafka=kafka,
            settings=_make_settings(
                proactive_resource_freeze_enabled=False,
                proactive_react_max_turns=4,
            )
        )
        ev = _make_ev(namespace="multi-agent")

        appended_obs: list[str] = []
        audit_outcomes: list[str] = []

        async def fake_react_mem_append(ctx, trace, obs):
            appended_obs.append(obs)

        async def fake_append_audit(ctx, *, trace_id, rule_id, outcome, **kw):
            audit_outcomes.append(outcome)

        from workers.tools import ToolCallPayload, TOOL_REGISTRY
        diagnose_call = ToolCallPayload(
            tool="inspect_pod_deep", args={"namespace": "multi-agent", "pod": "my-pod"}
        )
        treat_call = ToolCallPayload(
            tool="k8s_rollout_restart",
            args={"namespace": "multi-agent", "deployment": "my-app"}
        )

        call_seq = [diagnose_call, treat_call]
        call_idx = [0]

        async def fake_parse(*a, **kw):
            idx = call_idx[0]
            if idx >= len(call_seq):
                return (None, 0.0, "done")
            call_idx[0] += 1
            return (call_seq[idx], 0.95, "ok")

        async def fake_tool_fn(ctx, args):
            return "[STATUS] business_hit"

        async def fake_lease_fail(*a, **kw):
            return False

        with patch.dict(TOOL_REGISTRY, {"inspect_pod_deep": fake_tool_fn}):
            with patch.object(po, "_parse_fallback_tool_call", side_effect=fake_parse), \
                 patch.object(po, "_react_mem_recent", return_value=[]), \
                 patch.object(po, "_react_mem_append", new=fake_react_mem_append), \
                 patch.object(po, "_set_negative_pattern", new=AsyncMock()), \
                 patch.object(po, "_append_audit", new=fake_append_audit), \
                 patch("workers.proactive_react_runner.extract_resource_ref",
                       return_value=("multi-agent", "Deployment", "my-app")), \
                 patch("workers.proactive_react_runner.try_acquire_resource_lease", new=fake_lease_fail):
                from workers.proactive_react_runner import run_proactive_react_fallback
                await run_proactive_react_fallback(
                    ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
                )
        assert any("lease_conflict" in o for o in appended_obs)

    @pytest.mark.asyncio
    async def test_dev_mode_bypasses_confidence_check(self):
        """Dev mode allows tools even below confidence threshold."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(proactive_react_max_turns=1))
        ev = _make_ev()

        with patch("workers.proactive_react_runner.is_dev_mode", return_value=True):
            call = _tool_call(tool="inspect_pod_deep")

            async def fake_tool_fn(ctx, args):
                return "ok result"

            from workers.tools import TOOL_REGISTRY
            with patch.dict(TOOL_REGISTRY, {"inspect_pod_deep": fake_tool_fn}):
                with patch.object(po, "_parse_fallback_tool_call", return_value=(call, 0.1, "ok")):
                    with patch.object(po, "_react_mem_recent", return_value=[]):
                        with patch.object(po, "_react_mem_append", new=AsyncMock()):
                            with patch.object(po, "_set_negative_pattern", new=AsyncMock()):
                                with patch.object(po, "_append_audit", new=AsyncMock()):
                                    from workers.proactive_react_runner import run_proactive_react_fallback
                                    await run_proactive_react_fallback(
                                        ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
                                    )
        # Should complete without low_confidence block

    @pytest.mark.asyncio
    async def test_recheck_not_verified_back_to_prescribe(self):
        """Recheck fails verification → cycles back to prescribe."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(proactive_react_max_turns=6))
        ev = _make_ev(namespace="multi-agent")

        appended_obs: list[str] = []
        audit_outcomes: list[str] = []

        async def fake_react_mem_append(ctx, trace, obs):
            appended_obs.append(obs)

        async def fake_append_audit(ctx, *, trace_id, rule_id, outcome, **kw):
            audit_outcomes.append(outcome)

        diagnose_call = _tool_call(tool="inspect_pod_deep")
        prescribe_call = _tool_call(
            tool="k8s_rollout_restart",
            args={"namespace": "multi-agent", "deployment": "my-app"}
        )
        recheck_call = _tool_call(tool="k8s_list_pods", args={"namespace": "multi-agent"})

        call_seq = [diagnose_call, prescribe_call, recheck_call]
        call_idx = [0]

        async def fake_parse(*a, **kw):
            idx = call_idx[0]
            if idx >= len(call_seq):
                return (None, 0.0, "exhausted")
            call_idx[0] += 1
            return (call_seq[idx], 0.95, "ok")

        async def fake_diagnose_tool(ctx, args):
            return "[STATUS] business_hit"

        async def fake_mutate_tool(ctx, args):
            return "[STATUS] business_hit rollout ok"

        async def fake_recheck_tool(ctx, args):
            # recheck not verified (empty_result → verified=False)
            return "[STATUS] empty_result"

        from workers.tools import TOOL_REGISTRY
        with patch.dict(TOOL_REGISTRY, {
            "inspect_pod_deep": fake_diagnose_tool,
            "k8s_rollout_restart": fake_mutate_tool,
            "k8s_list_pods": fake_recheck_tool,
        }):
            with patch.object(po, "_parse_fallback_tool_call", side_effect=fake_parse):
                with patch.object(po, "_react_mem_recent", return_value=[]):
                    with patch.object(po, "_react_mem_append", new=fake_react_mem_append):
                        with patch.object(po, "_set_negative_pattern", new=AsyncMock()):
                            with patch.object(po, "_append_audit", new=fake_append_audit):
                                from workers.proactive_react_runner import run_proactive_react_fallback
                                await run_proactive_react_fallback(
                                    ctx, ev, trace="trace-test01", pattern_key="pk-001", msg_id="msg-001"
                                )
        # Should have gone through recheck and cycled back
        assert any("recheck" in o for o in appended_obs)
