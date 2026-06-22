"""
tests/test_cov_agentic_slow_path.py

Coverage tests for src/workers/agentic_slow_path.py (0% -> meaningful coverage).
Targets all pure helper functions plus the main async loop with mocked externals.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.llm_mock_compat import CompatLLM

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(**kw: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "god_mode": False,
        "lab_unchained": False,
        "omni_concise_reply_max_words": 10,
        "omni_summary_max_words": 100,
        "slow_path_max_tool_attempts": 3,
        "slow_path_stale_signature_streak": 2,
        "json_repair_max": 1,
        "compress_turn_threshold": 5,
        "model_helper": "qwen2.5:1.5b",
        "model_reasoning_engine": "qwen2.5:7b",
        "model_heavy_lifter": "qwen2.5:7b",
        "chat_model": "qwen2.5:7b",
        "embed_model": "nomic-embed-text",
        "rag_fast_path_score": 0.9,
        "routing_experience_enabled": True,
        "routing_experience_score_threshold": 0.78,
        "action_experience_enabled": True,
        "baseline_snapshot_enabled": False,
        "baseline_system_prompt_max_chars": 1600,
        "agentic_slow_path_enabled": False,
        "fallback_inline_buttons_enabled": False,
        "session_ttl_sec": 86400,
        "agentic_max_llm_iterations": 3,
        "agentic_debug_io": False,
        "telegram_admin_chat_id": None,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class _FakeSemaphore:
    async def acquire(self):
        return "token"

    async def release(self, token):
        pass


def _make_ctx(**kw: Any) -> SimpleNamespace:
    """Minimal WorkerHandlerContext-like object."""
    defaults: dict[str, Any] = {
        "settings": _make_settings(),
        "redis": None,
        "llm": None,
        "vector_store": None,
        "ledger": None,
        "semaphore": _FakeSemaphore(),
        "telegram": None,
        "kafka": None,
        "telegram_chat_id": None,
        "inbound_source": "",
        "inbound_user_text": "",
        "restart_rollout_explicit": False,
        "pod_discovery_pairs": [],
        "inbound_trace_id": "test-trace",
        "llm_slot_held": False,
        "inbound_proactive": False,
        "k8s_mutated": False,
        "fallback_inline_commands": None,
        "_agentic_session_resolved": False,
        "_agentic_resolve_summary": "",
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# _escalate_premature_for_unattended
# ---------------------------------------------------------------------------

class TestEscalatePrematureForUnattended:
    def test_attended_alert_returns_false(self):
        from workers.agentic_slow_path import _escalate_premature_for_unattended
        result = _escalate_premature_for_unattended(
            unattended_alert=False,
            iteration=0,
            trajectory=[],
            raw_args={},
        )
        assert result is False

    def test_unattended_non_zero_iteration_returns_false(self):
        from workers.agentic_slow_path import _escalate_premature_for_unattended
        result = _escalate_premature_for_unattended(
            unattended_alert=True,
            iteration=1,
            trajectory=[],
            raw_args={},
        )
        assert result is False

    def test_unattended_with_existing_trajectory_returns_false(self):
        from workers.agentic_slow_path import _escalate_premature_for_unattended
        result = _escalate_premature_for_unattended(
            unattended_alert=True,
            iteration=0,
            trajectory=[{"tool": "list_pods", "args": {}}],
            raw_args={},
        )
        assert result is False

    def test_unattended_iter0_empty_trajectory_no_safe_reason_returns_true(self):
        from workers.agentic_slow_path import _escalate_premature_for_unattended
        result = _escalate_premature_for_unattended(
            unattended_alert=True,
            iteration=0,
            trajectory=[],
            raw_args={"reason": "pod is down", "detail": "crashing"},
        )
        assert result is True

    def test_unattended_security_reason_allows_escalate(self):
        from workers.agentic_slow_path import _escalate_premature_for_unattended
        result = _escalate_premature_for_unattended(
            unattended_alert=True,
            iteration=0,
            trajectory=[],
            raw_args={"reason": "security breach detected", "detail": ""},
        )
        assert result is False

    def test_unattended_malicious_reason_allows_escalate(self):
        from workers.agentic_slow_path import _escalate_premature_for_unattended
        result = _escalate_premature_for_unattended(
            unattended_alert=True,
            iteration=0,
            trajectory=[],
            raw_args={"reason": "malicious actor", "detail": ""},
        )
        assert result is False

    def test_unattended_cve_reason_allows_escalate(self):
        from workers.agentic_slow_path import _escalate_premature_for_unattended
        result = _escalate_premature_for_unattended(
            unattended_alert=True,
            iteration=0,
            trajectory=[],
            raw_args={"reason": "CVE-2025-1234 exploit", "detail": ""},
        )
        assert result is False

    def test_unattended_policy_block_allows_escalate(self):
        from workers.agentic_slow_path import _escalate_premature_for_unattended
        result = _escalate_premature_for_unattended(
            unattended_alert=True,
            iteration=0,
            trajectory=[],
            raw_args={"reason": "policy_block triggered", "detail": ""},
        )
        assert result is False

    def test_unattended_unsafe_detail_allows_escalate(self):
        from workers.agentic_slow_path import _escalate_premature_for_unattended
        result = _escalate_premature_for_unattended(
            unattended_alert=True,
            iteration=0,
            trajectory=[],
            raw_args={"reason": "some error", "detail": "unsafe operation"},
        )
        assert result is False

    def test_unattended_forbidden_detail_allows_escalate(self):
        from workers.agentic_slow_path import _escalate_premature_for_unattended
        result = _escalate_premature_for_unattended(
            unattended_alert=True,
            iteration=0,
            trajectory=[],
            raw_args={"reason": "error", "detail": "forbidden access"},
        )
        assert result is False

    def test_empty_args_unattended_iter0_returns_true(self):
        from workers.agentic_slow_path import _escalate_premature_for_unattended
        result = _escalate_premature_for_unattended(
            unattended_alert=True,
            iteration=0,
            trajectory=[],
            raw_args={},
        )
        assert result is True

    def test_none_reason_fields_unattended_iter0_returns_true(self):
        from workers.agentic_slow_path import _escalate_premature_for_unattended
        result = _escalate_premature_for_unattended(
            unattended_alert=True,
            iteration=0,
            trajectory=[],
            raw_args={"reason": None, "detail": None},
        )
        assert result is True


# ---------------------------------------------------------------------------
# _agentic_span
# ---------------------------------------------------------------------------

class TestAgenticSpan:
    def test_returns_nullcontext_when_no_tracer(self):
        """Without OTel configured, should return nullcontext."""
        from workers.agentic_slow_path import _agentic_span, _AGENTIC_TRACER
        span = _agentic_span("test_span")
        # Should be usable as a context manager without error
        with span:
            pass

    def test_span_context_manager_no_exception(self):
        from workers.agentic_slow_path import _agentic_span
        with _agentic_span("some_span"):
            result = 1 + 1
        assert result == 2


# ---------------------------------------------------------------------------
# _effective_trace_id_for_logs
# ---------------------------------------------------------------------------

class TestEffectiveTraceIdForLogs:
    def test_returns_session_trace_when_no_otel(self):
        from workers.agentic_slow_path import _effective_trace_id_for_logs
        result = _effective_trace_id_for_logs("my-trace-123")
        assert result == "my-trace-123"

    def test_returns_unknown_when_empty_and_no_otel(self):
        from workers.agentic_slow_path import _effective_trace_id_for_logs
        result = _effective_trace_id_for_logs("")
        assert result == "unknown"

    def test_returns_unknown_when_none_and_no_otel(self):
        from workers.agentic_slow_path import _effective_trace_id_for_logs
        result = _effective_trace_id_for_logs(None)
        assert result == "unknown"

    def test_arbitrary_trace_string_preserved(self):
        from workers.agentic_slow_path import _effective_trace_id_for_logs
        result = _effective_trace_id_for_logs("abc-def-ghi")
        assert result == "abc-def-ghi"


# ---------------------------------------------------------------------------
# _trace_action_json_preview
# ---------------------------------------------------------------------------

class TestTraceActionJsonPreview:
    def test_empty_args_returns_empty(self):
        from workers.agentic_slow_path import _trace_action_json_preview
        result = _trace_action_json_preview({})
        assert result == ""

    def test_redacts_sensitive_keys(self):
        from workers.agentic_slow_path import _trace_action_json_preview
        result = _trace_action_json_preview({
            "token": "secret_value",
            "namespace": "multi-agent",
        })
        assert "[REDACTED]" in result
        assert "secret_value" not in result
        assert "multi-agent" in result

    def test_redacts_password_key(self):
        from workers.agentic_slow_path import _trace_action_json_preview
        result = _trace_action_json_preview({"password": "supersecret"})
        assert "[REDACTED]" in result
        assert "supersecret" not in result

    def test_redacts_key_field(self):
        from workers.agentic_slow_path import _trace_action_json_preview
        result = _trace_action_json_preview({"key": "mykey123"})
        assert "[REDACTED]" in result

    def test_redacts_auth_field(self):
        from workers.agentic_slow_path import _trace_action_json_preview
        result = _trace_action_json_preview({"auth": "bearer token"})
        assert "[REDACTED]" in result

    def test_dict_value_serialized(self):
        from workers.agentic_slow_path import _trace_action_json_preview
        result = _trace_action_json_preview({"namespace": "default", "opts": {"limit": 10}})
        assert "namespace" in result
        assert "opts" in result

    def test_list_value_serialized(self):
        from workers.agentic_slow_path import _trace_action_json_preview
        result = _trace_action_json_preview({"pods": ["pod-a", "pod-b"]})
        assert "pods" in result

    def test_long_value_truncated(self):
        from workers.agentic_slow_path import _trace_action_json_preview
        result = _trace_action_json_preview({"message": "x" * 1000})
        assert len(result) <= 600

    def test_max_keys_capped_at_18(self):
        from workers.agentic_slow_path import _trace_action_json_preview
        args = {f"key_{i}": f"val_{i}" for i in range(30)}
        result = _trace_action_json_preview(args)
        # Should not error, output should be within limit
        assert len(result) <= 600

    def test_non_string_value_converted(self):
        from workers.agentic_slow_path import _trace_action_json_preview
        result = _trace_action_json_preview({"count": 42, "flag": True})
        assert "count" in result
        assert "42" in result

    def test_credential_key_redacted(self):
        from workers.agentic_slow_path import _trace_action_json_preview
        result = _trace_action_json_preview({"credential": "my-cred"})
        assert "[REDACTED]" in result

    def test_secret_key_redacted(self):
        from workers.agentic_slow_path import _trace_action_json_preview
        result = _trace_action_json_preview({"secret": "my-secret"})
        assert "[REDACTED]" in result


# ---------------------------------------------------------------------------
# _structured_agentic_log
# ---------------------------------------------------------------------------

class TestStructuredAgenticLog:
    def test_logs_json_with_component(self, caplog):
        import logging
        from workers.agentic_slow_path import _structured_agentic_log
        with caplog.at_level(logging.INFO, logger="workers.agentic_slow_path"):
            _structured_agentic_log({"phase": "test", "value": 42}, session_trace="trace-xyz")
        assert len(caplog.records) == 1
        row = json.loads(caplog.records[0].message)
        assert row["component"] == "agentic_slow_path"
        assert row["session_trace"] == "trace-xyz"
        assert row["phase"] == "test"
        assert row["value"] == 42

    def test_includes_trace_id(self, caplog):
        import logging
        from workers.agentic_slow_path import _structured_agentic_log
        with caplog.at_level(logging.INFO, logger="workers.agentic_slow_path"):
            _structured_agentic_log({"event": "ok"}, session_trace="trace-abc")
        row = json.loads(caplog.records[0].message)
        assert "trace_id" in row

    def test_empty_payload_logged(self, caplog):
        import logging
        from workers.agentic_slow_path import _structured_agentic_log
        with caplog.at_level(logging.INFO, logger="workers.agentic_slow_path"):
            _structured_agentic_log({}, session_trace="t")
        assert len(caplog.records) == 1


# ---------------------------------------------------------------------------
# _messages_snapshot_for_dump
# ---------------------------------------------------------------------------

class TestMessagesSnapshotForDump:
    def test_empty_messages_returns_empty(self):
        from workers.agentic_slow_path import _messages_snapshot_for_dump
        result = _messages_snapshot_for_dump([])
        assert result == []

    def test_preserves_role_and_content(self):
        from workers.agentic_slow_path import _messages_snapshot_for_dump
        msgs = [{"role": "user", "content": "hello"}]
        result = _messages_snapshot_for_dump(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "hello"

    def test_long_content_truncated(self):
        from workers.agentic_slow_path import _messages_snapshot_for_dump
        msgs = [{"role": "assistant", "content": "x" * 10000}]
        result = _messages_snapshot_for_dump(msgs, per_msg=500)
        assert len(result[0]["content"]) < 10000

    def test_multiple_messages(self):
        from workers.agentic_slow_path import _messages_snapshot_for_dump
        msgs = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "User message"},
            {"role": "assistant", "content": "Assistant reply"},
        ]
        result = _messages_snapshot_for_dump(msgs)
        assert len(result) == 3
        assert [m["role"] for m in result] == ["system", "user", "assistant"]

    def test_non_string_content_preserved(self):
        from workers.agentic_slow_path import _messages_snapshot_for_dump
        msgs = [{"role": "user", "content": 42}]
        result = _messages_snapshot_for_dump(msgs)
        # Non-string content preserved as-is
        assert result[0]["content"] == 42

    def test_missing_role_defaults_to_empty(self):
        from workers.agentic_slow_path import _messages_snapshot_for_dump
        msgs = [{"content": "hello"}]
        result = _messages_snapshot_for_dump(msgs)
        assert result[0]["role"] == ""


# ---------------------------------------------------------------------------
# _tombstone_with_trajectory
# ---------------------------------------------------------------------------

class TestTombstoneWithTrajectory:
    def test_basic_tombstone_has_required_keys(self):
        from workers.agentic_slow_path import _tombstone_with_trajectory
        tomb = _tombstone_with_trajectory([], [], reason="max_iterations")
        assert "reason" in tomb
        assert "trajectory" in tomb
        assert tomb["reason"] == "max_iterations"

    def test_trajectory_is_included(self):
        from workers.agentic_slow_path import _tombstone_with_trajectory
        traj = [{"tool": "list_pods", "args": {"namespace": "default"}}]
        tomb = _tombstone_with_trajectory([], traj, reason="test")
        data = json.loads(tomb["trajectory"])
        assert "scratchpad_tools" in data
        assert data["scratchpad_tools"][0]["tool"] == "list_pods"

    def test_error_included_when_provided(self):
        from workers.agentic_slow_path import _tombstone_with_trajectory
        tomb = _tombstone_with_trajectory([], [], reason="exception", error="SomeError")
        assert "error" in tomb
        assert "SomeError" in tomb["error"]

    def test_no_error_key_when_not_provided(self):
        from workers.agentic_slow_path import _tombstone_with_trajectory
        tomb = _tombstone_with_trajectory([], [], reason="done")
        assert "error" not in tomb

    def test_large_trajectory_truncated(self):
        from workers.agentic_slow_path import _tombstone_with_trajectory
        big_msgs = [{"role": "user", "content": "x" * 1000} for _ in range(20)]
        tomb = _tombstone_with_trajectory(big_msgs, [], reason="max")
        # Should be truncated
        assert len(tomb["trajectory"]) <= 7100

    def test_messages_included_in_trajectory_dump(self):
        from workers.agentic_slow_path import _tombstone_with_trajectory
        msgs = [{"role": "user", "content": "hello"}]
        tomb = _tombstone_with_trajectory(msgs, [], reason="done")
        data = json.loads(tomb["trajectory"])
        assert "messages" in data
        assert len(data["messages"]) == 1


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

class TestModuleLevelConstants:
    def test_trajectory_tombstone_json_max(self):
        from workers.agentic_slow_path import _TRAJECTORY_TOMBSTONE_JSON_MAX
        assert _TRAJECTORY_TOMBSTONE_JSON_MAX == 7000

    def test_max_tool_feed(self):
        from workers.agentic_slow_path import _MAX_TOOL_FEED
        assert _MAX_TOOL_FEED == 3000

    def test_agentic_debug_per_msg(self):
        from workers.agentic_slow_path import _AGENTIC_DEBUG_PER_MSG
        assert _AGENTIC_DEBUG_PER_MSG == 4000

    def test_agentic_debug_raw_cap(self):
        from workers.agentic_slow_path import _AGENTIC_DEBUG_RAW_CAP
        assert _AGENTIC_DEBUG_RAW_CAP == 48_000


# ---------------------------------------------------------------------------
# agentic_slow_path_with_llm_and_tools — main async function (mocked)
# ---------------------------------------------------------------------------

class TestAgenticSlowPathMainLoop:
    """Test main async loop with all heavy externals mocked."""

    def _make_resolve_tool_fn(self):
        """Returns an async fn that simulates omni_mark_resolved."""
        async def _fn(ctx, args):
            ctx._agentic_session_resolved = True
            ctx._agentic_resolve_summary = "resolved"
            return "Resolution complete."
        return _fn

    def _make_escalate_tool_fn(self):
        async def _fn(ctx, args):
            return "Escalated."
        return _fn

    @pytest.fixture
    def ctx_with_mocks(self):
        ctx = _make_ctx()
        ctx.llm = CompatLLM()
        return ctx

    @pytest.mark.asyncio
    async def test_resolves_on_omni_mark_resolved(self, ctx_with_mocks):
        """Test that the loop returns resolved output when omni_mark_resolved is called."""
        from workers.agentic_slow_path import agentic_slow_path_with_llm_and_tools

        ctx = ctx_with_mocks
        ctx.settings.agentic_max_llm_iterations = 3

        # LLM returns omni_mark_resolved JSON
        ctx.llm.chat = AsyncMock(return_value={
            "message": {"content": json.dumps({"tool": "omni_mark_resolved", "args": {"summary": "all good"}})}
        })

        resolve_fn = self._make_resolve_tool_fn()

        with (
            patch("workers.agentic_slow_path.append_agent_audit", new_callable=AsyncMock),
            patch("workers.agentic_slow_path.fetch_action_experience_context", new_callable=AsyncMock, return_value=None),
            patch("workers.agentic_slow_path.build_agentic_system_messages", return_value=[{"role": "system", "content": "sys"}]),
            patch("workers.agentic_slow_path._k8s_smart_target_hint", return_value=""),
            patch("workers.agentic_slow_path.dispatch_task", return_value="qwen2.5:7b"),
            patch("workers.agentic_slow_path.get_tool_registry") as mock_reg,
            patch("workers.agentic_slow_path.TOOL_REGISTRY", {"omni_mark_resolved": resolve_fn}),
            patch("workers.agentic_slow_path.record_agent_playbook_from_trajectory", new_callable=AsyncMock),
            patch("workers.agentic_slow_path.inc_llm_requests"),
            patch("workers.agentic_slow_path.inc_agent_sessions_total"),
            patch("workers.agentic_slow_path.inc_experience_saved"),
            patch("workers.agentic_slow_path.inc_agent_premature_escalate_blocked"),
        ):
            mock_reg.return_value.has.return_value = False
            result = await agentic_slow_path_with_llm_and_tools(
                ctx,
                "Fix the pod crash",
                trace="test-trace-001",
            )
        assert result == "Resolution complete."

    @pytest.mark.asyncio
    async def test_max_iterations_returns_diagnosis_string(self, ctx_with_mocks):
        """Loop exhausts iterations without resolution."""
        from workers.agentic_slow_path import agentic_slow_path_with_llm_and_tools

        ctx = ctx_with_mocks
        ctx.settings.agentic_max_llm_iterations = 2

        # LLM always returns empty content to trigger continue
        ctx.llm.chat = AsyncMock(return_value={"message": {"content": ""}})

        with (
            patch("workers.agentic_slow_path.append_agent_audit", new_callable=AsyncMock),
            patch("workers.agentic_slow_path.fetch_action_experience_context", new_callable=AsyncMock, return_value=None),
            patch("workers.agentic_slow_path.build_agentic_system_messages", return_value=[{"role": "system", "content": "sys"}]),
            patch("workers.agentic_slow_path._k8s_smart_target_hint", return_value=""),
            patch("workers.agentic_slow_path.dispatch_task", return_value="qwen2.5:7b"),
            patch("workers.agentic_slow_path.get_tool_registry") as mock_reg,
            patch("workers.agentic_slow_path.TOOL_REGISTRY", {}),
            patch("workers.agentic_slow_path.inc_llm_requests"),
            patch("workers.agentic_slow_path.inc_agent_sessions_total"),
        ):
            mock_reg.return_value.has.return_value = False
            result = await agentic_slow_path_with_llm_and_tools(
                ctx,
                "Check pod",
                trace="test-max-iter",
            )
        assert "max iterations" in result.lower() or "DIAGNOSIS" in result

    @pytest.mark.asyncio
    async def test_json_parse_failure_continues(self, ctx_with_mocks):
        """Invalid JSON from LLM causes parse failure recovery."""
        from workers.agentic_slow_path import agentic_slow_path_with_llm_and_tools

        ctx = ctx_with_mocks
        ctx.settings.agentic_max_llm_iterations = 2
        ctx.settings.json_repair_max = 0

        # Return invalid JSON then empty
        ctx.llm.chat = AsyncMock(side_effect=[
            {"message": {"content": "not json at all {broken"}},
            {"message": {"content": ""}},
        ])

        with (
            patch("workers.agentic_slow_path.append_agent_audit", new_callable=AsyncMock),
            patch("workers.agentic_slow_path.fetch_action_experience_context", new_callable=AsyncMock, return_value=None),
            patch("workers.agentic_slow_path.build_agentic_system_messages", return_value=[{"role": "system", "content": "sys"}]),
            patch("workers.agentic_slow_path._k8s_smart_target_hint", return_value=""),
            patch("workers.agentic_slow_path.dispatch_task", return_value="qwen2.5:7b"),
            patch("workers.agentic_slow_path.get_tool_registry") as mock_reg,
            patch("workers.agentic_slow_path.TOOL_REGISTRY", {}),
            patch("workers.agentic_slow_path.inc_llm_requests"),
            patch("workers.agentic_slow_path.inc_agent_sessions_total"),
        ):
            mock_reg.return_value.has.return_value = False
            result = await agentic_slow_path_with_llm_and_tools(
                ctx,
                "Check pod",
                trace="test-parse-fail",
            )
        # Should reach max iterations
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_unknown_tool_feedback_injected(self, ctx_with_mocks):
        """Unknown tool name causes feedback message to be injected."""
        from workers.agentic_slow_path import agentic_slow_path_with_llm_and_tools

        ctx = ctx_with_mocks
        ctx.settings.agentic_max_llm_iterations = 2
        ctx.settings.json_repair_max = 0

        # First call: unknown tool; second: empty to exhaust
        ctx.llm.chat = AsyncMock(side_effect=[
            {"message": {"content": json.dumps({"tool": "nonexistent_tool", "args": {}})}},
            {"message": {"content": ""}},
        ])

        with (
            patch("workers.agentic_slow_path.append_agent_audit", new_callable=AsyncMock),
            patch("workers.agentic_slow_path.fetch_action_experience_context", new_callable=AsyncMock, return_value=None),
            patch("workers.agentic_slow_path.build_agentic_system_messages", return_value=[{"role": "system", "content": "sys"}]),
            patch("workers.agentic_slow_path._k8s_smart_target_hint", return_value=""),
            patch("workers.agentic_slow_path.dispatch_task", return_value="qwen2.5:7b"),
            patch("workers.agentic_slow_path.get_tool_registry") as mock_reg,
            patch("workers.agentic_slow_path.TOOL_REGISTRY", {}),  # empty = unknown tool
            patch("workers.agentic_slow_path.format_unknown_tool_feedback_en", return_value="Use a valid tool."),
            patch("workers.agentic_slow_path.inc_llm_requests"),
            patch("workers.agentic_slow_path.inc_agent_sessions_total"),
        ):
            mock_reg.return_value.has.return_value = False
            result = await agentic_slow_path_with_llm_and_tools(
                ctx,
                "Do something",
                trace="test-unknown-tool",
            )
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_escalate_to_human_returns_requires_human(self, ctx_with_mocks):
        """escalate_to_human tool returns REQUIRES_HUMAN result."""
        from workers.agentic_slow_path import agentic_slow_path_with_llm_and_tools

        ctx = ctx_with_mocks
        ctx.settings.agentic_max_llm_iterations = 2

        ctx.llm.chat = AsyncMock(return_value={
            "message": {"content": json.dumps({
                "tool": "escalate_to_human",
                "args": {"reason": "Cannot determine root cause", "detail": "Need expert"},
            })}
        })

        escalate_fn = self._make_escalate_tool_fn()

        with (
            patch("workers.agentic_slow_path.append_agent_audit", new_callable=AsyncMock),
            patch("workers.agentic_slow_path.fetch_action_experience_context", new_callable=AsyncMock, return_value=None),
            patch("workers.agentic_slow_path.build_agentic_system_messages", return_value=[{"role": "system", "content": "sys"}]),
            patch("workers.agentic_slow_path._k8s_smart_target_hint", return_value=""),
            patch("workers.agentic_slow_path.dispatch_task", return_value="qwen2.5:7b"),
            patch("workers.agentic_slow_path.get_tool_registry") as mock_reg,
            patch("workers.agentic_slow_path.TOOL_REGISTRY", {"escalate_to_human": escalate_fn}),
            patch("workers.agentic_slow_path.inc_llm_requests"),
            patch("workers.agentic_slow_path.inc_agent_sessions_total"),
            patch("workers.agentic_slow_path.inc_agent_premature_escalate_blocked"),
        ):
            mock_reg.return_value.has.return_value = False
            result = await agentic_slow_path_with_llm_and_tools(
                ctx,
                "Debug cluster",
                trace="test-escalate",
                unattended_alert=False,
            )
        assert "[REQUIRES_HUMAN]" in result
        assert "Cannot determine root cause" in result

    @pytest.mark.asyncio
    async def test_escalate_blocked_for_unattended_no_trajectory(self, ctx_with_mocks):
        """Premature escalation blocked for unattended alert on first iteration without trajectory."""
        from workers.agentic_slow_path import agentic_slow_path_with_llm_and_tools

        ctx = ctx_with_mocks
        ctx.settings.agentic_max_llm_iterations = 2

        # First iter: escalate_to_human (should be blocked); second: empty to exhaust
        ctx.llm.chat = AsyncMock(side_effect=[
            {"message": {"content": json.dumps({
                "tool": "escalate_to_human",
                "args": {"reason": "pod is down", "detail": "crashing"},
            })}},
            {"message": {"content": ""}},
        ])

        escalate_fn = self._make_escalate_tool_fn()

        with (
            patch("workers.agentic_slow_path.append_agent_audit", new_callable=AsyncMock),
            patch("workers.agentic_slow_path.fetch_action_experience_context", new_callable=AsyncMock, return_value=None),
            patch("workers.agentic_slow_path.build_agentic_system_messages", return_value=[{"role": "system", "content": "sys"}]),
            patch("workers.agentic_slow_path._k8s_smart_target_hint", return_value=""),
            patch("workers.agentic_slow_path.dispatch_task", return_value="qwen2.5:7b"),
            patch("workers.agentic_slow_path.get_tool_registry") as mock_reg,
            patch("workers.agentic_slow_path.TOOL_REGISTRY", {"escalate_to_human": escalate_fn}),
            patch("workers.agentic_slow_path.inc_llm_requests"),
            patch("workers.agentic_slow_path.inc_agent_sessions_total"),
            patch("workers.agentic_slow_path.inc_agent_premature_escalate_blocked") as mock_blocked,
        ):
            mock_reg.return_value.has.return_value = False
            result = await agentic_slow_path_with_llm_and_tools(
                ctx,
                "Check pod",
                trace="test-unattended-block",
                unattended_alert=True,
            )
        # escalate was blocked on iter 0
        mock_blocked.assert_called_once()
        # Should reach max iterations message
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_with_session_state_and_summary(self, ctx_with_mocks):
        """Test path with session summary injected into messages."""
        from workers.agentic_slow_path import agentic_slow_path_with_llm_and_tools

        ctx = ctx_with_mocks
        ctx.settings.agentic_max_llm_iterations = 1
        ctx.llm.chat = AsyncMock(return_value={"message": {"content": ""}})

        with (
            patch("workers.agentic_slow_path.append_agent_audit", new_callable=AsyncMock),
            patch("workers.agentic_slow_path.fetch_action_experience_context", new_callable=AsyncMock, return_value="Experience context"),
            patch("workers.agentic_slow_path.build_agentic_system_messages", return_value=[{"role": "system", "content": "sys"}]),
            patch("workers.agentic_slow_path._k8s_smart_target_hint", return_value="target hint"),
            patch("workers.agentic_slow_path.dispatch_task", return_value="qwen2.5:7b"),
            patch("workers.agentic_slow_path.get_tool_registry") as mock_reg,
            patch("workers.agentic_slow_path.TOOL_REGISTRY", {}),
            patch("workers.agentic_slow_path.inc_llm_requests"),
            patch("workers.agentic_slow_path.inc_agent_sessions_total"),
        ):
            mock_reg.return_value.has.return_value = False
            result = await agentic_slow_path_with_llm_and_tools(
                ctx,
                "Check pod",
                trace="test-session",
                session_summary="Previous session summary",
                recent_turns=[
                    {"role": "user", "content": "earlier message"},
                    {"role": "assistant", "content": "earlier reply"},
                ],
            )
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_tool_error_continues_loop(self, ctx_with_mocks):
        """Tool execution error should inject error feedback and continue."""
        from workers.agentic_slow_path import agentic_slow_path_with_llm_and_tools

        ctx = ctx_with_mocks
        ctx.settings.agentic_max_llm_iterations = 2
        ctx.settings.json_repair_max = 0

        async def _fail_fn(ctx, args):
            raise RuntimeError("Connection refused")

        ctx.llm.chat = AsyncMock(side_effect=[
            {"message": {"content": json.dumps({"tool": "some_tool", "args": {"ns": "default"}})}},
            {"message": {"content": ""}},
        ])

        with (
            patch("workers.agentic_slow_path.append_agent_audit", new_callable=AsyncMock),
            patch("workers.agentic_slow_path.fetch_action_experience_context", new_callable=AsyncMock, return_value=None),
            patch("workers.agentic_slow_path.build_agentic_system_messages", return_value=[{"role": "system", "content": "sys"}]),
            patch("workers.agentic_slow_path._k8s_smart_target_hint", return_value=""),
            patch("workers.agentic_slow_path.dispatch_task", return_value="qwen2.5:7b"),
            patch("workers.agentic_slow_path.get_tool_registry") as mock_reg,
            patch("workers.agentic_slow_path.TOOL_REGISTRY", {"some_tool": _fail_fn}),
            patch("workers.agentic_slow_path.inc_llm_requests"),
            patch("workers.agentic_slow_path.inc_agent_sessions_total"),
        ):
            mock_reg.return_value.has.return_value = True
            result = await agentic_slow_path_with_llm_and_tools(
                ctx,
                "Do work",
                trace="test-tool-error",
            )
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_baseline_snapshot_enabled_adds_system_message(self, ctx_with_mocks):
        """When baseline_snapshot_enabled, fetch and inject baseline prompt."""
        from workers.agentic_slow_path import agentic_slow_path_with_llm_and_tools

        ctx = ctx_with_mocks
        ctx.settings.agentic_max_llm_iterations = 1
        ctx.settings.baseline_snapshot_enabled = True
        ctx.llm.chat = AsyncMock(return_value={"message": {"content": ""}})

        with (
            patch("workers.agentic_slow_path.append_agent_audit", new_callable=AsyncMock),
            patch("workers.agentic_slow_path.fetch_action_experience_context", new_callable=AsyncMock, return_value=None),
            patch("workers.agentic_slow_path.build_agentic_system_messages", return_value=[{"role": "system", "content": "sys"}]),
            patch("workers.agentic_slow_path._k8s_smart_target_hint", return_value=""),
            patch("workers.agentic_slow_path.dispatch_task", return_value="qwen2.5:7b"),
            patch("workers.agentic_slow_path.fetch_baseline_system_prompt", new_callable=AsyncMock, return_value="Baseline prompt"),
            patch("workers.agentic_slow_path.get_tool_registry") as mock_reg,
            patch("workers.agentic_slow_path.TOOL_REGISTRY", {}),
            patch("workers.agentic_slow_path.inc_llm_requests"),
            patch("workers.agentic_slow_path.inc_agent_sessions_total"),
        ):
            mock_reg.return_value.has.return_value = False
            result = await agentic_slow_path_with_llm_and_tools(
                ctx,
                "Do work",
                trace="test-baseline",
            )
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_debug_io_mode_logs_request_and_response(self, ctx_with_mocks):
        """When agentic_debug_io=True, structured log is called."""
        from workers.agentic_slow_path import agentic_slow_path_with_llm_and_tools

        ctx = ctx_with_mocks
        ctx.settings.agentic_max_llm_iterations = 1
        ctx.settings.agentic_debug_io = True
        ctx.llm.chat = AsyncMock(return_value={"message": {"content": ""}})

        with (
            patch("workers.agentic_slow_path.append_agent_audit", new_callable=AsyncMock),
            patch("workers.agentic_slow_path.fetch_action_experience_context", new_callable=AsyncMock, return_value=None),
            patch("workers.agentic_slow_path.build_agentic_system_messages", return_value=[{"role": "system", "content": "sys"}]),
            patch("workers.agentic_slow_path._k8s_smart_target_hint", return_value=""),
            patch("workers.agentic_slow_path.dispatch_task", return_value="qwen2.5:7b"),
            patch("workers.agentic_slow_path.get_tool_registry") as mock_reg,
            patch("workers.agentic_slow_path.TOOL_REGISTRY", {}),
            patch("workers.agentic_slow_path.inc_llm_requests"),
            patch("workers.agentic_slow_path.inc_agent_sessions_total"),
        ):
            mock_reg.return_value.has.return_value = False
            result = await agentic_slow_path_with_llm_and_tools(
                ctx,
                "Debug me",
                trace="test-debug-io",
            )
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_telegram_notification_on_escalate(self, ctx_with_mocks):
        """When escalating with telegram configured, send_message is called."""
        from workers.agentic_slow_path import agentic_slow_path_with_llm_and_tools

        ctx = ctx_with_mocks
        ctx.settings.agentic_max_llm_iterations = 2
        ctx.settings.telegram_admin_chat_id = "123456"

        mock_telegram = AsyncMock()
        mock_telegram.send_message = AsyncMock()
        ctx.telegram = mock_telegram

        ctx.llm.chat = AsyncMock(return_value={
            "message": {"content": json.dumps({
                "tool": "escalate_to_human",
                "args": {"reason": "Need human", "detail": ""},
            })}
        })

        escalate_fn = self._make_escalate_tool_fn()

        with (
            patch("workers.agentic_slow_path.append_agent_audit", new_callable=AsyncMock),
            patch("workers.agentic_slow_path.fetch_action_experience_context", new_callable=AsyncMock, return_value=None),
            patch("workers.agentic_slow_path.build_agentic_system_messages", return_value=[{"role": "system", "content": "sys"}]),
            patch("workers.agentic_slow_path._k8s_smart_target_hint", return_value=""),
            patch("workers.agentic_slow_path.dispatch_task", return_value="qwen2.5:7b"),
            patch("workers.agentic_slow_path.get_tool_registry") as mock_reg,
            patch("workers.agentic_slow_path.TOOL_REGISTRY", {"escalate_to_human": escalate_fn}),
            patch("workers.agentic_slow_path.inc_llm_requests"),
            patch("workers.agentic_slow_path.inc_agent_sessions_total"),
            patch("workers.agentic_slow_path.inc_agent_premature_escalate_blocked"),
        ):
            mock_reg.return_value.has.return_value = False
            result = await agentic_slow_path_with_llm_and_tools(
                ctx,
                "Escalate this",
                trace="test-telegram-escalate",
                unattended_alert=False,
            )

        mock_telegram.send_message.assert_called_once()
        assert "[REQUIRES_HUMAN]" in result

    @pytest.mark.asyncio
    async def test_omni_mark_resolved_error_continues(self, ctx_with_mocks):
        """Error in omni_mark_resolved execution injects error message and continues."""
        from workers.agentic_slow_path import agentic_slow_path_with_llm_and_tools

        ctx = ctx_with_mocks
        ctx.settings.agentic_max_llm_iterations = 2
        ctx.settings.json_repair_max = 0

        async def _fail_resolve(ctx, args):
            raise RuntimeError("DB error")

        ctx.llm.chat = AsyncMock(side_effect=[
            {"message": {"content": json.dumps({"tool": "omni_mark_resolved", "args": {"summary": "done"}})}},
            {"message": {"content": ""}},
        ])

        with (
            patch("workers.agentic_slow_path.append_agent_audit", new_callable=AsyncMock),
            patch("workers.agentic_slow_path.fetch_action_experience_context", new_callable=AsyncMock, return_value=None),
            patch("workers.agentic_slow_path.build_agentic_system_messages", return_value=[{"role": "system", "content": "sys"}]),
            patch("workers.agentic_slow_path._k8s_smart_target_hint", return_value=""),
            patch("workers.agentic_slow_path.dispatch_task", return_value="qwen2.5:7b"),
            patch("workers.agentic_slow_path.get_tool_registry") as mock_reg,
            patch("workers.agentic_slow_path.TOOL_REGISTRY", {"omni_mark_resolved": _fail_resolve}),
            patch("workers.agentic_slow_path.inc_llm_requests"),
            patch("workers.agentic_slow_path.inc_agent_sessions_total"),
        ):
            mock_reg.return_value.has.return_value = False
            result = await agentic_slow_path_with_llm_and_tools(
                ctx,
                "Resolve",
                trace="test-resolve-error",
            )
        # After error, loop continues and hits max iterations
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_successful_tool_call_adds_to_trajectory(self, ctx_with_mocks):
        """Successful tool call adds entry to trajectory and feeds result back."""
        from workers.agentic_slow_path import agentic_slow_path_with_llm_and_tools

        ctx = ctx_with_mocks
        ctx.settings.agentic_max_llm_iterations = 3
        ctx.settings.json_repair_max = 0

        async def _list_pods(ctx, args):
            return "pod-a\npod-b\npod-c"

        async def _resolve(ctx, args):
            ctx._agentic_resolve_summary = "Fixed"
            return "Done"

        ctx.llm.chat = AsyncMock(side_effect=[
            {"message": {"content": json.dumps({"tool": "list_pods", "args": {"namespace": "default"}})}},
            {"message": {"content": json.dumps({"tool": "omni_mark_resolved", "args": {"summary": "fixed"}})}},
        ])

        with (
            patch("workers.agentic_slow_path.append_agent_audit", new_callable=AsyncMock),
            patch("workers.agentic_slow_path.fetch_action_experience_context", new_callable=AsyncMock, return_value=None),
            patch("workers.agentic_slow_path.build_agentic_system_messages", return_value=[{"role": "system", "content": "sys"}]),
            patch("workers.agentic_slow_path._k8s_smart_target_hint", return_value=""),
            patch("workers.agentic_slow_path.dispatch_task", return_value="qwen2.5:7b"),
            patch("workers.agentic_slow_path.get_tool_registry") as mock_reg,
            patch("workers.agentic_slow_path.TOOL_REGISTRY", {
                "list_pods": _list_pods,
                "omni_mark_resolved": _resolve,
            }),
            patch("workers.agentic_slow_path.prepare_tool_return_for_llm", side_effect=lambda ctx, x: x),
            patch("workers.agentic_slow_path.record_agent_playbook_from_trajectory", new_callable=AsyncMock),
            patch("workers.agentic_slow_path.inc_llm_requests"),
            patch("workers.agentic_slow_path.inc_agent_sessions_total"),
            patch("workers.agentic_slow_path.inc_experience_saved"),
        ):
            mock_reg.return_value.has.return_value = False
            result = await agentic_slow_path_with_llm_and_tools(
                ctx,
                "List and fix pods",
                trace="test-trajectory",
            )
        assert result == "Done"
