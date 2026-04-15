"""Agentic ReAct slow-path: multi-iteration JSON tools; learn only on omni_mark_resolved."""

from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from typing import Any

from execution.experience import (
    fetch_action_experience_context,
    record_agent_playbook_from_trajectory,
)
from workers.agent_audit import append_agent_audit
from workers.baseline_snapshot import fetch_baseline_system_prompt
from workers.handlers import (
    WorkerHandlerContext,
    _k8s_smart_target_hint,
    _parse_tool_json,
    _repair_json_with_helper,
    build_agentic_system_messages,
)
from workers.metrics_exporter import (
    inc_agent_premature_escalate_blocked,
    inc_agent_sessions_total,
    inc_experience_saved,
    inc_llm_requests,
)
from workers.model_routing import dispatch_task
from workers.session_state import SessionState
from workers.slow_path_trace import truncate_for_prompt
from workers.tool_observation import prepare_tool_return_for_llm
from workers.tool_registry import get_tool_registry
from workers.tools import TOOL_REGISTRY, ToolCallPayload, format_unknown_tool_feedback_en

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace as _otel_trace

    _AGENTIC_TRACER = _otel_trace.get_tracer(__name__)
except Exception:  # pragma: no cover - optional OTel
    _AGENTIC_TRACER = None

_TRAJECTORY_TOMBSTONE_JSON_MAX = 7000

_MAX_TOOL_FEED = 3000
# Khi OMNI_AGENTIC_DEBUG_IO=1: giới hạn độ dài mỗi message trong snapshot (tránh log vài MB).
_AGENTIC_DEBUG_PER_MSG = 4000
_AGENTIC_DEBUG_RAW_CAP = 48_000


def _escalate_premature_for_unattended(
    *,
    unattended_alert: bool,
    iteration: int,
    trajectory: list[dict[str, Any]],
    raw_args: dict[str, Any],
) -> bool:
    """Unattended alert: không cho escalate lượt đầu khi chưa có tool quan sát (business fail nếu bỏ qua)."""
    if not unattended_alert or iteration != 0:
        return False
    if trajectory:
        return False
    reason = str(raw_args.get("reason") or "").lower()
    detail = str(raw_args.get("detail") or "").lower()
    blob = f"{reason} {detail}"
    # Cho phép leo thang ngay chỉ khi lý do an toàn / policy — không phải 'thiếu tên pod'
    if any(k in blob for k in ("security", "malicious", "unsafe", "cve", "policy_block", "forbidden")):
        return False
    return True


def _agentic_span(name: str):
    if _AGENTIC_TRACER is None:
        return nullcontext()
    return _AGENTIC_TRACER.start_as_current_span(name)


def _effective_trace_id_for_logs(session_trace: str) -> str:
    if _AGENTIC_TRACER is None:
        return session_trace or "unknown"
    try:
        sc = _otel_trace.get_current_span().get_span_context()
        if sc and getattr(sc, "is_valid", False):
            return format(sc.trace_id, "032x")
    except Exception:
        pass
    return session_trace or "unknown"


def _trace_action_json_preview(args: dict[str, Any]) -> str:
    """Short redacted preview for TRACE_ACTION_JSON (no secrets)."""
    parts: list[str] = []
    for k in sorted(args.keys())[:18]:
        sk = str(k)
        if any(x in sk.lower() for x in ("token", "password", "secret", "key", "auth", "credential")):
            parts.append(f"{sk}=[REDACTED]")
            continue
        v = args[k]
        if isinstance(v, (dict, list)):
            s = json.dumps(v, ensure_ascii=False)[:220]
        else:
            s = str(v)[:240]
        parts.append(f"{sk}={s!r}")
    return " ".join(parts)[:520]


def _structured_agentic_log(payload: dict[str, Any], *, session_trace: str) -> None:
    row = {
        "component": "agentic_slow_path",
        "trace_id": _effective_trace_id_for_logs(session_trace),
        "session_trace": session_trace,
        **payload,
    }
    logger.info(json.dumps(row, ensure_ascii=False, default=str))


def _messages_snapshot_for_dump(
    messages: list[dict[str, Any]], *, per_msg: int = 2000
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "")
        c = m.get("content", "")
        if isinstance(c, str):
            c = truncate_for_prompt(c, per_msg)
        out.append({"role": role, "content": c})
    return out


def _tombstone_with_trajectory(
    messages: list[dict[str, Any]],
    tool_trajectory: list[dict[str, Any]],
    *,
    reason: str,
    error: str | None = None,
) -> dict[str, Any]:
    hist = {
        "messages": _messages_snapshot_for_dump(messages),
        "scratchpad_tools": list(tool_trajectory),
    }
    raw = json.dumps(hist, ensure_ascii=False, default=str)
    if len(raw) > _TRAJECTORY_TOMBSTONE_JSON_MAX:
        raw = raw[: _TRAJECTORY_TOMBSTONE_JSON_MAX - 20].rstrip() + "…[truncated]"
    tomb: dict[str, Any] = {
        "reason": reason,
        "trajectory": raw,
    }
    if error:
        tomb["error"] = truncate_for_prompt(error, 1500)
    return tomb


async def agentic_slow_path_with_llm_and_tools(
    ctx: WorkerHandlerContext,
    user_text: str,
    *,
    trace: str = "unknown",
    session_summary: str = "",
    recent_turns: list[dict[str, str]] | None = None,
    needs_plan: bool = False,
    state: SessionState | None = None,
    unattended_alert: bool = False,
) -> str:
    """ReAct loop: tool OK feeds back into messages; exit on omni_mark_resolved or max iterations."""
    from workers.handlers import _compress_history, _deepseek_plan

    await append_agent_audit(
        ctx,
        phase="agentic",
        trace_id=trace,
        event="session_start",
        user_len=len(user_text or ""),
    )

    actx = await fetch_action_experience_context(ctx, user_text)
    logger.info("[%s] agentic_acquire", trace)
    token = await ctx.semaphore.acquire()
    ctx.llm_slot_held = True
    ctx._agentic_session_resolved = False
    ctx._agentic_resolve_summary = ""

    trajectory: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    json_parse_failures = 0

    try:
        if state is not None and state.turn_count > ctx.settings.compress_turn_threshold:
            state.last_summary = await _compress_history(ctx, state, trace)
            state.turn_count = 0
            logger.info("[%s] agentic_session_compressed", trace)

        execution_plan = ""
        if needs_plan:
            execution_plan = await _deepseek_plan(ctx, user_text, trace)
            logger.info("[%s] agentic_plan len=%s", trace, len(execution_plan))

        use_executor_7b = bool((execution_plan or "").strip())

        messages = build_agentic_system_messages(ctx, unattended_alert=unattended_alert)
        if ctx.settings.baseline_snapshot_enabled:
            baseline_sys = await fetch_baseline_system_prompt(
                ctx.redis, ctx.settings.baseline_system_prompt_max_chars
            )
            if baseline_sys:
                messages.append({"role": "system", "content": baseline_sys})
        if actx:
            messages.append({"role": "system", "content": actx})
        if (session_summary or "").strip():
            messages.append({"role": "system", "content": f"[SUMMARY]\n{session_summary.strip()}"})
        extra = _k8s_smart_target_hint(user_text)
        if extra:
            messages.append({"role": "system", "content": extra})
        if (execution_plan or "").strip():
            messages.append({"role": "system", "content": f"[PLAN]\n{execution_plan.strip()}"})
        if recent_turns:
            for m in recent_turns:
                role = m.get("role")
                content = (m.get("content") or "").strip()
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})

        max_iter = ctx.settings.agentic_max_llm_iterations

        for iteration in range(max_iter):
            with _agentic_span(f"iter_{iteration}"):
                model = (
                    ctx.settings.model_heavy_lifter
                    if use_executor_7b
                    else dispatch_task(
                        model_default=ctx.settings.chat_model,
                        model_reasoning=ctx.settings.model_reasoning_engine,
                        model_heavy=ctx.settings.model_heavy_lifter,
                        user_text=user_text,
                        attempt=iteration,
                        json_parse_failures=json_parse_failures,
                    )
                )
                logger.info("[%s] agentic_chat iter=%s/%s model=%s", trace, iteration + 1, max_iter, model)
                if getattr(ctx.settings, "agentic_debug_io", False):
                    _structured_agentic_log(
                        {
                            "phase": "llm_request",
                            "iteration": iteration,
                            "model": model,
                            "message_count": len(messages),
                            "messages": _messages_snapshot_for_dump(
                                messages, per_msg=_AGENTIC_DEBUG_PER_MSG
                            ),
                        },
                        session_trace=trace,
                    )
                inc_llm_requests()
                with _agentic_span("llm_generate"):
                    resp = await ctx.llm.chat(
                        model=model,
                        messages=messages,
                        options={"temperature": 0.1, "num_ctx": 4096},
                        format="json",
                    )
                content = (resp.get("message") or {}).get("content") or ""
                if getattr(ctx.settings, "agentic_debug_io", False):
                    _structured_agentic_log(
                        {
                            "phase": "llm_response",
                            "iteration": iteration,
                            "model": model,
                            "char_len": len(content),
                            "raw_content": truncate_for_prompt(content, _AGENTIC_DEBUG_RAW_CAP),
                        },
                        session_trace=trace,
                    )
                if not content.strip():
                    await append_agent_audit(
                        ctx,
                        phase="agentic",
                        trace_id=trace,
                        event="empty_model",
                        iteration=iteration,
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": "[SYSTEM] Empty model output — try another JSON tool or omni_mark_resolved if done.",
                        }
                    )
                    continue

                call: ToolCallPayload | None = None
                text = content
                last_parse_err = ""
                try:
                    max_rep = ctx.settings.json_repair_max
                    for repair_i in range(max_rep + 1):
                        try:
                            call = _parse_tool_json(text)
                            break
                        except Exception as e:
                            last_parse_err = str(e)
                            if repair_i >= max_rep:
                                raise
                            text = await _repair_json_with_helper(
                                ctx, content, parse_error=last_parse_err
                            )
                except Exception as e2:
                    json_parse_failures += 1
                    await append_agent_audit(
                        ctx,
                        phase="agentic",
                        trace_id=trace,
                        event="parse_fail",
                        err=str(e2)[:500],
                        iteration=iteration,
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": f"[SYSTEM] Invalid JSON: {truncate_for_prompt(str(e2), 400)}",
                        }
                    )
                    continue

                assert call is not None
                _ta_args = call.args if isinstance(call.args, dict) else {}
                logger.info(
                    "event=TRACE_ACTION_JSON trace_id=%s tool=%s args_preview=%s",
                    trace,
                    call.tool,
                    _trace_action_json_preview(_ta_args),
                )
                fn = TOOL_REGISTRY.get(call.tool)
                if not fn:
                    await append_agent_audit(
                        ctx,
                        phase="agentic",
                        trace_id=trace,
                        event="unknown_tool",
                        tool=call.tool,
                        iteration=iteration,
                    )
                    logger.warning(
                        "[%s] unknown_tool name=%r not in TOOL_REGISTRY — re-prompting with English catalog",
                        trace,
                        call.tool,
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": format_unknown_tool_feedback_en(
                                call.tool, unattended=unattended_alert
                            ),
                        }
                    )
                    continue

                raw_args = call.args if isinstance(call.args, dict) else {}
                thought = truncate_for_prompt(content, 4000)

                if call.tool == "escalate_to_human":
                    if _escalate_premature_for_unattended(
                        unattended_alert=unattended_alert,
                        iteration=iteration,
                        trajectory=trajectory,
                        raw_args=raw_args,
                    ):
                        inc_agent_premature_escalate_blocked()
                        await append_agent_audit(
                            ctx,
                            phase="agentic",
                            trace_id=trace,
                            event="escalate_blocked_discovery_required",
                            iteration=iteration,
                            business_outcome="premature_escalate_blocked",
                        )
                        _structured_agentic_log(
                            {
                                "phase": "escalate_blocked",
                                "iteration": iteration,
                                "business_outcome": "premature_escalate_blocked",
                                "action_name": call.tool,
                                "detail": "Unattended alert requires ≥1 observation tool before escalate (tech-only success is not business success).",
                            },
                            session_trace=trace,
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "[SYSTEM] Business rule: this alert has no workload id in FACTS yet — you MUST run "
                                    "exactly one discovery/observation tool first, e.g. "
                                    '`{"tool":"list_all_pods_sdk","args":{"limit":200}}` or '
                                    '`{"tool":"promql_instant","args":{"query":"up"}}` or '
                                    '`{"tool":"query_prometheus_metrics","args":{...}}`. '
                                    "Do not use escalate_to_human until after `[TOOL_RESULT]` from such a tool. "
                                    "Reply with one JSON tool only (no markdown, no ```)."
                                ),
                            }
                        )
                        continue
                    _structured_agentic_log(
                        {
                            "phase": "tool_before",
                            "iteration": iteration,
                            "thought": thought,
                            "action_name": call.tool,
                            "action_args": dict(raw_args),
                        },
                        session_trace=trace,
                    )
                    reason = str(raw_args.get("reason") or "").strip() or "unspecified"
                    detail = str(raw_args.get("detail") or "").strip()[:2000]
                    traj_esc = trajectory + [{"tool": "escalate_to_human", "args": dict(raw_args)}]
                    tomb = _tombstone_with_trajectory(
                        messages,
                        traj_esc,
                        reason="escalate_to_human",
                    )
                    tomb["escalation_reason"] = truncate_for_prompt(reason, 500)
                    tomb["session_trace"] = trace
                    await append_agent_audit(
                        ctx,
                        phase="agentic",
                        trace_id=trace,
                        event="escalate_to_human",
                        outcome="REQUIRES_HUMAN_INTERVENTION",
                        tombstone=tomb,
                        iteration=iteration,
                    )
                    ws = ctx.settings
                    if ctx.telegram is not None and ws.telegram_admin_chat_id is not None:
                        try:
                            await ctx.telegram.send_message(
                                int(ws.telegram_admin_chat_id),
                                f"[REQUIRES_HUMAN] trace={trace} tool=escalate_to_human\n"
                                f"reason={reason[:1500]}\n{detail[:1000]}",
                            )
                        except Exception as e:
                            logger.warning("[%s] escalate telegram: %s", trace, e)
                    inc_agent_sessions_total()
                    _structured_agentic_log(
                        {
                            "phase": "tool_after",
                            "iteration": iteration,
                            "action_name": call.tool,
                            "observation": truncate_for_prompt(reason + " " + detail, 4000),
                        },
                        session_trace=trace,
                    )
                    return (
                        f"[REQUIRES_HUMAN] {reason}"
                        + (f"\n{detail}" if detail else "")
                    ).strip()

                if call.tool == "omni_mark_resolved":
                    _structured_agentic_log(
                        {
                            "phase": "tool_before",
                            "iteration": iteration,
                            "thought": thought,
                            "action_name": call.tool,
                            "action_args": dict(raw_args),
                        },
                        session_trace=trace,
                    )
                    with _agentic_span("tool_execute"):
                        try:
                            out = await fn(ctx, raw_args)
                        except Exception as e:
                            _structured_agentic_log(
                                {
                                    "phase": "tool_after_error",
                                    "iteration": iteration,
                                    "action_name": call.tool,
                                    "error": repr(e),
                                },
                                session_trace=trace,
                            )
                            await append_agent_audit(
                                ctx,
                                phase="agentic",
                                trace_id=trace,
                                event="mark_resolved_error",
                                err=repr(e),
                            )
                            messages.append(
                                {
                                    "role": "user",
                                    "content": f"[SYSTEM] omni_mark_resolved error: {truncate_for_prompt(repr(e), 400)}",
                                }
                            )
                            continue
                    _structured_agentic_log(
                        {
                            "phase": "tool_after",
                            "iteration": iteration,
                            "action_name": call.tool,
                            "observation": truncate_for_prompt(str(out), 4000),
                        },
                        session_trace=trace,
                    )
                    trajectory.append({"tool": "omni_mark_resolved", "args": dict(raw_args)})
                    summary = str(getattr(ctx, "_agentic_resolve_summary", "") or "").strip()
                    await record_agent_playbook_from_trajectory(
                        ctx,
                        user_text=user_text,
                        trajectory=trajectory,
                        trace_id=trace,
                        resolution_summary=summary,
                    )
                    inc_experience_saved()
                    await append_agent_audit(
                        ctx,
                        phase="agentic",
                        trace_id=trace,
                        event="resolved",
                        summary_len=len(summary),
                        steps=len(trajectory),
                    )
                    inc_agent_sessions_total()
                    return out

                _structured_agentic_log(
                    {
                        "phase": "tool_before",
                        "iteration": iteration,
                        "thought": thought,
                        "action_name": call.tool,
                        "action_args": dict(raw_args),
                    },
                    session_trace=trace,
                )
                with _agentic_span("tool_execute"):
                    try:
                        out = await fn(ctx, raw_args)
                        if not get_tool_registry().has(call.tool):
                            out = prepare_tool_return_for_llm(ctx, out)
                    except Exception as e:
                        _structured_agentic_log(
                            {
                                "phase": "tool_after_error",
                                "iteration": iteration,
                                "action_name": call.tool,
                                "error": repr(e),
                            },
                            session_trace=trace,
                        )
                        await append_agent_audit(
                            ctx,
                            phase="agentic",
                            trace_id=trace,
                            event="tool_error",
                            tool=call.tool,
                            err=repr(e),
                            iteration=iteration,
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": f"[TOOL_ERROR] `{call.tool}`: {truncate_for_prompt(repr(e), 600)}",
                            }
                        )
                        continue

                _structured_agentic_log(
                    {
                        "phase": "tool_after",
                        "iteration": iteration,
                        "action_name": call.tool,
                        "observation": truncate_for_prompt(str(out), 4000),
                    },
                    session_trace=trace,
                )

                trajectory.append({"tool": call.tool, "args": dict(raw_args)})
                await append_agent_audit(
                    ctx,
                    phase="agentic",
                    trace_id=trace,
                    event="tool_ok",
                    tool=call.tool,
                    iteration=iteration,
                )

                feed = truncate_for_prompt(str(out), _MAX_TOOL_FEED)
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": f"[TOOL_RESULT] tool={call.tool}\n{feed}",
                    }
                )

        tomb = _tombstone_with_trajectory(
            messages,
            trajectory,
            reason="max_iterations",
        )
        await append_agent_audit(
            ctx,
            phase="agentic",
            trace_id=trace,
            event="max_iterations",
            max_iter=max_iter,
            outcome="REQUIRES_HUMAN_INTERVENTION",
            tombstone=tomb,
        )
        return (
            "[DIAGNOSIS] Agentic max iterations — `omni_mark_resolved` not called. "
            "Retry with a clearer scope or raise OMNI_AGENTIC_MAX_LLM_ITERATIONS."
        )
    except Exception as agentic_exc:
        tomb = _tombstone_with_trajectory(
            messages,
            trajectory,
            reason="agentic_exception",
            error=repr(agentic_exc),
        )
        await append_agent_audit(
            ctx,
            phase="agentic",
            trace_id=trace,
            event="agentic_fatal",
            outcome="REQUIRES_HUMAN_INTERVENTION",
            tombstone=tomb,
            err=repr(agentic_exc)[:800],
        )
        raise
    finally:
        ctx.llm_slot_held = False
        await ctx.semaphore.release(token)
