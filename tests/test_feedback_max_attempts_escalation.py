"""Tests: feedback loop terminates with MAX_MUTATE_ATTEMPTS after exhausting retries."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest

from workers.handler_context import WorkerHandlerContext


_STATE_KEY = "omni:autonomous:state:{trace}"
_CTX_KEY = "omni:autonomous:ctx:{trace}"


def _make_ctx(
    *,
    max_attempts: int = 3,
    max_verify: int = 3,
) -> WorkerHandlerContext:
    ws = SimpleNamespace(
        autonomous_execute_max_attempts=max_attempts,
        autonomous_verify_max_rounds=max_verify,
        omni_post_mutate_sdk_verify_enabled=False,
        omni_post_mutate_verify_planner_enabled=False,
        omni_sdk_verify_max_rounds=3,
        omni_sdk_verify_initial_delay_sec=0,
        omni_post_verify_deployment_state_enabled=False,
        omni_feedback_full_agentic_planner_enabled=False,
        omni_llm_first_autonomy_enabled=False,
        omni_unrestricted_tool_execution=True,
        omni_legacy_deterministic_fallback=False,
        lab_chaos_credential_autofix_enabled=False,
        omni_experience_requires_sdk_verify=False,
        omni_state_verify_max_attempts=2,
        omni_state_verify_initial_delay_sec=0,
        kafka_topic_audit_agent="omni-audit",
        kafka_topic_action_feedback="omni-action-feedback",
        kafka_topic_actions="omni-actions",
        kafka_topic_hitl_pending="omni-hitl-pending",
        diag_evidence_llm_model="qwen2.5-coder-3b",
        model_helper="qwen2.5-coder-3b",
        model_reasoning_engine="qwen2.5:7b",
        telegram_admin_chat_id=None,
        default_remediation_namespace="multi-agent",
        omni_sigma_log_bypass_enabled=False,
        omni_shadow_os_mode=False,
    )
    kafka_mock = MagicMock()
    kafka_mock.send_dict = AsyncMock()
    return WorkerHandlerContext(
        settings=ws,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        llm=AsyncMock(),
        vector_store=MagicMock(),
        ledger=MagicMock(),
        semaphore=AsyncMock(),
        telegram=None,
        kafka=kafka_mock,
    )


def _feedback_envelope(
    trace: str,
    tool_name: str = "k8s_rollout_restart",
    exit_code: int = 1,
    stdout: str = "still degraded",
    stderr: str = "timeout",
    skipped_reason: str = "",
) -> dict[str, str]:
    body = {
        "trace_id": trace,
        "tool_name": tool_name,
        "mutate_args": {"namespace": "ns", "deployment": "dep"},
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "skipped_reason": skipped_reason,
        "status": "failed" if exit_code != 0 else "ok",
    }
    return {"trace_id": trace, "data": json.dumps(body)}


# ---------------------------------------------------------------------------
# A) Last attempt at limit → MAX_MUTATE_ATTEMPTS tombstone
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_attempts_emits_terminal_tombstone():
    """
    When last_attempt_count == max_attempts (3) and executor fails (exit_code=1),
    handle_action_feedback_envelope must emit MAX_MUTATE_ATTEMPTS tombstone.
    """
    from workers.autonomous_feedback_loop import handle_action_feedback_envelope

    trace = "test-max-att-001"
    ctx = _make_ctx(max_attempts=3)

    # Seed Redis: last_attempt_count = 3 (at the limit)
    state = {"last_attempt_count": 3, "feedback_failures": 0, "sdk_verify_round": 0}
    await ctx.redis.set(_STATE_KEY.format(trace=trace), json.dumps(state))
    # No ctx key → _finalize_if_deployment_rollout_healthy_from_stored_ctx returns False

    tombstones: list[dict] = []

    async def _capture_tombstone(ctx_, *, trace_id, reason_code, component, detail="", meta=None):
        tombstones.append({"trace_id": trace_id, "reason_code": reason_code})

    with (
        patch("workers.autonomous_feedback_loop.emit_terminal_tombstone", side_effect=_capture_tombstone),
        patch("workers.autonomous_feedback_loop.emit_telegram_escalation", new_callable=AsyncMock),
        patch("workers.autonomous_feedback_loop.emit_transition", new_callable=AsyncMock),
    ):
        envelope = _feedback_envelope(trace, exit_code=1)
        await handle_action_feedback_envelope(ctx, envelope)

    assert tombstones, "Expected at least one terminal tombstone"
    reason_codes = {t["reason_code"] for t in tombstones}
    assert "MAX_MUTATE_ATTEMPTS" in reason_codes, (
        f"Expected MAX_MUTATE_ATTEMPTS in tombstone reason codes, got: {reason_codes}"
    )


# ---------------------------------------------------------------------------
# B) last_attempt < max_attempts → does NOT emit MAX_MUTATE_ATTEMPTS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_below_max_attempts_no_tombstone():
    """
    When last_attempt_count=1 (< max_attempts=3), should NOT emit MAX_MUTATE_ATTEMPTS.
    """
    from workers.autonomous_feedback_loop import handle_action_feedback_envelope

    trace = "test-max-att-002"
    ctx = _make_ctx(max_attempts=3)

    state = {"last_attempt_count": 1, "feedback_failures": 0, "sdk_verify_round": 0}
    await ctx.redis.set(_STATE_KEY.format(trace=trace), json.dumps(state))

    tombstones: list[dict] = []

    async def _capture_tombstone(ctx_, *, trace_id, reason_code, component, detail="", meta=None):
        tombstones.append({"trace_id": trace_id, "reason_code": reason_code})

    with (
        patch("workers.autonomous_feedback_loop.emit_terminal_tombstone", side_effect=_capture_tombstone),
        patch("workers.autonomous_feedback_loop.emit_telegram_escalation", new_callable=AsyncMock),
        patch("workers.autonomous_feedback_loop.emit_transition", new_callable=AsyncMock),
        patch("workers.autonomous_feedback_loop.emit_execute_mutate", new_callable=AsyncMock),
        patch("workers.autonomous_feedback_loop._llm_replan_after_feedback", new_callable=AsyncMock, return_value=None),
        patch("workers.autonomous_feedback_loop.deterministic_mutate_plan_from_batch", return_value=None),
    ):
        envelope = _feedback_envelope(trace, exit_code=1)
        await handle_action_feedback_envelope(ctx, envelope)

    max_att_tombstones = [t for t in tombstones if t["reason_code"] == "MAX_MUTATE_ATTEMPTS"]
    assert not max_att_tombstones, (
        f"Should NOT emit MAX_MUTATE_ATTEMPTS at attempt 1/3, got: {tombstones}"
    )


# ---------------------------------------------------------------------------
# C) feedback_failures exceeds max_verify → MAX_VERIFY_ROUNDS tombstone
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_verify_rounds_emits_tombstone():
    """
    When feedback_failures > max_verify (4 > 3), emit MAX_VERIFY_ROUNDS tombstone.
    """
    from workers.autonomous_feedback_loop import handle_action_feedback_envelope

    trace = "test-max-att-003"
    ctx = _make_ctx(max_attempts=3, max_verify=3)

    # feedback_failures = 4 (will be incremented to 5 → > 3)
    state = {"last_attempt_count": 1, "feedback_failures": 4, "sdk_verify_round": 0}
    await ctx.redis.set(_STATE_KEY.format(trace=trace), json.dumps(state))

    tombstones: list[dict] = []

    async def _capture_tombstone(ctx_, *, trace_id, reason_code, component, detail="", meta=None):
        tombstones.append({"trace_id": trace_id, "reason_code": reason_code})

    with (
        patch("workers.autonomous_feedback_loop.emit_terminal_tombstone", side_effect=_capture_tombstone),
        patch("workers.autonomous_feedback_loop.emit_telegram_escalation", new_callable=AsyncMock),
        patch("workers.autonomous_feedback_loop.emit_transition", new_callable=AsyncMock),
    ):
        envelope = _feedback_envelope(trace, exit_code=1)
        await handle_action_feedback_envelope(ctx, envelope)

    reason_codes = {t["reason_code"] for t in tombstones}
    assert "MAX_VERIFY_ROUNDS" in reason_codes, (
        f"Expected MAX_VERIFY_ROUNDS tombstone, got: {reason_codes}"
    )
