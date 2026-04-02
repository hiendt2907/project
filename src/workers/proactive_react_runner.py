"""Proactive fallback ReAct phase loop (diagnose → prescribe → treat → recheck)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from workers.handlers import WorkerHandlerContext
from workers.llm_context_budget import truncate_for_llm
from workers.metrics_exporter import (
    inc_learning_upsert,
    inc_llm_requests,
    inc_proactive_fallback,
    inc_proactive_lease_conflict,
    inc_proactive_outcome,
    inc_proactive_skip_frozen,
    inc_proactive_verify,
)
from workers.otel_tracing import child_span
from workers.proactive_guardrails import (
    DEFAULT_LEASE_PREFIX,
    PROACTIVE_MUTATE_TOOLS,
    extract_resource_ref,
    is_namespace_frozen_fallback,
    is_resource_frozen,
    proactive_lease_key,
    try_acquire_resource_lease,
)
from workers.proactive_models import AnomalyEvent
from workers.proactive_tool_policy import PROACTIVE_DIAGNOSE_TOOLS, PROACTIVE_RECHECK_TOOLS
from workers.tool_observation import prepare_tool_return_for_llm
from workers.tools import TOOL_REGISTRY, ToolCallPayload

logger = logging.getLogger(__name__)


async def run_proactive_react_fallback(
    ctx: WorkerHandlerContext,
    ev: AnomalyEvent,
    *,
    trace: str,
    pattern_key: str,
    msg_id: str,
) -> None:
    """Bounded ReAct tool loop after governance allow; metrics/audit unchanged from historical behavior."""
    import workers.proactive_observer as po

    ws = ctx.settings
    if ws.proactive_fallback_bypass_policy_in_god_mode and (
        ws.god_mode or ws.lab_unchained or getattr(ws, "cluster_full_access", False)
    ):
        allowed_tools = set(TOOL_REGISTRY.keys())
    else:
        allowed_tools = {t.strip() for t in ws.proactive_fallback_allow_tools.split(",") if t.strip()}

    max_iterations = max(1, int(getattr(ws, "proactive_react_max_turns", 6) or 6))
    observations: list[str] = []
    final_reason = "max_iterations_exhausted"
    resolved = False
    escalated = False
    resolved_tool = ""
    resolved_confidence = 0.0
    resolved_output = ""
    last_call: ToolCallPayload | None = None
    phase = "diagnose"
    diagnose_tools = PROACTIVE_DIAGNOSE_TOOLS & allowed_tools
    prescribe_tools = PROACTIVE_MUTATE_TOOLS & allowed_tools
    recheck_tools = PROACTIVE_RECHECK_TOOLS & allowed_tools
    pending_treatment: ToolCallPayload | None = None
    pending_conf = 0.0
    pending_reason = ""
    last_treat_args: dict[str, Any] = {}
    last_treat_tool = ""
    last_treat_output = ""
    last_treat_verified = False
    for iteration in range(1, max_iterations + 1):
        mem_rows = await po._react_mem_recent(ctx, trace, limit=8)
        mem_cap = int(getattr(ws, "proactive_react_memory_max_chars", 3200) or 3200)
        obs_block = truncate_for_llm("\n".join(mem_rows[-5:]), mem_cap, tail=True)
        if phase == "diagnose":
            phase_tools = sorted(diagnose_tools or allowed_tools)
            instruction = "Giai đoạn diagnose: chọn 1 tool chẩn đoán để lấy bằng chứng."
        elif phase == "prescribe":
            phase_tools = sorted(prescribe_tools or PROACTIVE_MUTATE_TOOLS)
            instruction = (
                "Giai đoạn prescribe: dựa trên react_memory, chọn DUY NHẤT 1 tool điều trị (mutate). "
                "Tuyệt đối không chọn diagnostic/reply/echo/forecast."
            )
        elif phase == "recheck":
            phase_tools = sorted(recheck_tools or diagnose_tools or allowed_tools)
            instruction = "Giai đoạn recheck: chọn tool kiểm tra lại sau điều trị."
        else:
            phase_tools = sorted(allowed_tools)
            instruction = "Chọn 1 tool phù hợp."
        if not phase_tools:
            escalated = True
            final_reason = f"no_tools_for_phase_{phase}"
            break

        if phase == "treat":
            if pending_treatment is None:
                phase = "prescribe"
                continue
            call = pending_treatment
            confidence = pending_conf
            reason = pending_reason
        else:
            prompt = (
                f"rule={ev.rule_name}\n"
                f"metric_value={ev.metric_value}\nthreshold={ev.threshold}\n"
                f"canonical_query={ev.canonical_query[:1000]}\n"
                f"trigger_promql={getattr(ev, 'trigger_promql', '')[:1000]}\n"
                f"error_hint={getattr(ev, 'error_hint', '')}\n"
                f"baseline_promql={ws.proactive_promql[:1000]}\n"
                f"phase={phase}\n"
                f"phase_allowed_tools={phase_tools}\n"
                f"react_memory=\n{obs_block or '(none)'}\n"
                "Neu promql_instant tra [STATUS] empty_result hoac placeholder: "
                "doi sang metric kube_*/node_*/up that hoac dung query_prometheus_metrics/k8s_list_pods; "
                "cam dung chuoi 'metric_value > threshold' hoac ten bien tu prompt.\n"
                f"{instruction}"
            )
            prompt_cap = int(getattr(ws, "proactive_llm_prompt_max_chars", 4096) or 4096)
            prompt = truncate_for_llm(prompt, prompt_cap, tail=False)
            with child_span("proactive_parse_fallback_tool"):
                call, confidence, reason = await po._parse_fallback_tool_call(ctx, prompt)
            inc_llm_requests()
            if call is None:
                obs = f"iter#{iteration}:{phase}: parse_fail reason={reason}"
                observations.append(obs)
                await po._react_mem_append(ctx, trace, obs)
                inc_proactive_fallback("parse_fail")
                continue
            if call.tool not in set(phase_tools):
                obs = f"iter#{iteration}:{phase}: phase_policy_deny tool={call.tool}"
                observations.append(obs)
                await po._react_mem_append(ctx, trace, obs)
                inc_proactive_fallback("policy_deny")
                continue
            if (
                not (
                    ws.proactive_fallback_bypass_policy_in_god_mode
                    and (ws.god_mode or ws.lab_unchained or getattr(ws, "cluster_full_access", False))
                )
                and confidence < ws.proactive_fallback_confidence_min
            ):
                obs = f"iter#{iteration}:{phase}: low_confidence tool={call.tool} confidence={confidence:.3f}"
                observations.append(obs)
                await po._react_mem_append(ctx, trace, obs)
                inc_proactive_fallback("low_confidence")
                continue
            if phase == "prescribe":
                pending_treatment = call
                pending_conf = confidence
                pending_reason = reason
                obs = f"iter#{iteration}:prescribe selected_treatment tool={call.tool} conf={confidence:.2f}"
                observations.append(obs)
                await po._react_mem_append(ctx, trace, obs)
                phase = "treat"
                continue

        last_call = call
        ref = extract_resource_ref(call.tool, call.args)
        if ref and ws.proactive_resource_freeze_enabled:
            ns, kind, name = ref
            if await is_resource_frozen(
                ctx.redis,
                key_prefix=ws.proactive_freeze_key_prefix,
                namespace=ns,
                kind=kind,
                name=name,
            ):
                obs = f"iter#{iteration}:{phase}: resource_frozen tool={call.tool}"
                observations.append(obs)
                await po._react_mem_append(ctx, trace, obs)
                inc_proactive_skip_frozen("resource")
                if phase == "treat":
                    phase = "prescribe"
                continue
            if await is_namespace_frozen_fallback(
                ctx.redis, key_prefix=ws.proactive_freeze_key_prefix, namespace=ns
            ):
                obs = f"iter#{iteration}:{phase}: namespace_frozen tool={call.tool}"
                observations.append(obs)
                await po._react_mem_append(ctx, trace, obs)
                inc_proactive_skip_frozen("namespace")
                if phase == "treat":
                    phase = "prescribe"
                continue

        if ref and call.tool in PROACTIVE_MUTATE_TOOLS:
            lk = proactive_lease_key(DEFAULT_LEASE_PREFIX, call.tool, ref)
            lease_ok = await try_acquire_resource_lease(
                ctx.redis,
                lk,
                token=trace,
                ttl_sec=ws.proactive_lease_ttl_sec,
            )
            if not lease_ok:
                obs = f"iter#{iteration}:{phase}: lease_conflict tool={call.tool}"
                observations.append(obs)
                await po._react_mem_append(ctx, trace, obs)
                inc_proactive_lease_conflict()
                if phase == "treat":
                    phase = "prescribe"
                continue

        try:
            fn = TOOL_REGISTRY[call.tool]
            with child_span("proactive_tool_execute", tool_name=call.tool):
                out_fb = await asyncio.wait_for(
                    fn(ctx, call.args),
                    timeout=ws.proactive_tool_timeout_sec,
                )
            out_raw = str(out_fb)
            react_mx = getattr(ws, "proactive_react_tool_output_max_chars", None)
            react_cap: int | None = int(react_mx) if react_mx is not None and int(react_mx) > 0 else None
            out_fb_s = prepare_tool_return_for_llm(ctx, out_raw, max_chars=react_cap)
            verified = po._quick_verify_output(out_fb_s, ws.proactive_verify_keywords_fail)
            status_now = po._result_status(out_fb_s)
            actionable_fix = bool(verified and call.tool in PROACTIVE_MUTATE_TOOLS)
            inc_proactive_fallback("success" if verified else "verify_fail")
            inc_proactive_verify("success" if verified else "fail")
            obs = (
                f"iter#{iteration}:{phase}: tool={call.tool} verified={verified} "
                f"actionable_fix={actionable_fix} status={status_now} obs={out_fb_s[:280]}"
            )
            observations.append(obs)
            await po._react_mem_append(ctx, trace, obs)
            po._dbg_log(
                run_id=f"trace-{trace}",
                hypothesis_id="H14",
                location="proactive_react_runner.py:run_proactive_react_fallback",
                message="react_iteration_result",
                data={
                    "iteration": iteration,
                    "phase": phase,
                    "tool": call.tool,
                    "verified": verified,
                    "actionable_fix": actionable_fix,
                    "result_status": status_now,
                },
            )
            await po._append_audit(
                ctx,
                trace_id=trace,
                rule_id=ev.rule_name,
                outcome="REACT_ITERATION_OK" if verified else "REACT_ITERATION_FAIL",
                commands_run=call.tool,
                detail=out_fb_s[:2000],
                meta={
                    "path": "fallback_react",
                    "pattern_key": pattern_key,
                    "iteration": iteration,
                    "phase": phase,
                    "confidence": round(confidence, 4),
                    "reason": reason,
                    "result_status": status_now,
                    "actionable_fix": actionable_fix,
                },
            )
            if phase == "diagnose":
                phase = "prescribe"
                continue
            if phase == "treat":
                last_treat_tool = call.tool
                last_treat_args = dict(call.args or {})
                last_treat_output = out_fb_s
                last_treat_verified = verified
                pending_treatment = None
                phase = "recheck"
                continue
            if phase == "recheck":
                if verified and last_treat_verified and last_treat_tool in PROACTIVE_MUTATE_TOOLS:
                    resolved = True
                    resolved_tool = last_treat_tool
                    resolved_confidence = pending_conf
                    resolved_output = f"treat={last_treat_output[:1200]}\nrecheck={out_fb_s[:1200]}"
                    allow_upsert = po._allow_learning_upsert(last_treat_tool, last_treat_output, True)
                    inc_learning_upsert("proactive_fallback", "success" if allow_upsert else "fail")
                    if allow_upsert and last_treat_args:
                        await po._save_proactive_learning_record(
                            ctx,
                            trace_id=trace,
                            pattern_key=pattern_key,
                            lesson=f"[proactive fallback react] tool={last_treat_tool} query={ev.canonical_query[:300]}",
                            tool=last_treat_tool,
                            args=last_treat_args,
                            exec_outcome="success",
                            biz_outcome="correct",
                            verification_result="pass",
                            unknown_reason="",
                        )
                    break
                phase = "prescribe"
                continue
        except asyncio.TimeoutError as e:
            obs = f"iter#{iteration}:{phase}: timeout tool={call.tool}"
            observations.append(obs)
            await po._react_mem_append(ctx, trace, obs)
            await po._fail_safe_after_tool_error(
                ctx,
                ev,
                trace,
                pattern_key,
                call,
                e,
                reason_code="TOOL_TIMEOUT",
                stream_msg_id=msg_id,
            )
            escalated = True
            final_reason = "tool_timeout"
            break
        except Exception as e:
            obs = f"iter#{iteration}:{phase}: exception tool={call.tool}"
            observations.append(obs)
            await po._react_mem_append(ctx, trace, obs)
            await po._fail_safe_after_tool_error(
                ctx,
                ev,
                trace,
                pattern_key,
                call,
                e,
                reason_code="TOOL_EXCEPTION",
                stream_msg_id=msg_id,
            )
            escalated = True
            final_reason = "tool_exception"
            break

    esc_reason = final_reason if escalated else "max_iterations_exhausted"
    if resolved:
        inc_proactive_outcome("react_resolved")
    else:
        inc_proactive_outcome("react_escalated")
        await po._set_negative_pattern(ctx, pattern_key, esc_reason)

    if ctx.telegram and ws.telegram_admin_chat_id:
        try:
            if resolved:
                safe_res = po._sanitize_proactive_telegram_body(resolved_output)
                await ctx.telegram.send_message(
                    int(ws.telegram_admin_chat_id),
                    f"[PROACTIVE][RESOLVED] trace={trace} tool={resolved_tool} conf={resolved_confidence:.2f}\n"
                    f"{safe_res[:3000]}",
                )
            else:
                obs_tail = po._sanitize_proactive_telegram_body("\n".join(observations[-4:]))
                await ctx.telegram.send_message(
                    int(ws.telegram_admin_chat_id),
                    f"[PROACTIVE][ESCALATED] trace={trace} reason={esc_reason}\n"
                    f"last_call={(last_call.tool if last_call else 'none')}\n"
                    f"observations:\n{obs_tail[:3200]}",
                )
        except Exception as e:
            logger.warning("[%s] proactive telegram react-final: %s", trace, e)

    if resolved:
        await po._append_audit(
            ctx,
            trace_id=trace,
            rule_id=ev.rule_name,
            outcome="RESOLVED",
            commands_run=resolved_tool,
            detail=resolved_output[:2000],
            meta={"path": "fallback_react", "pattern_key": pattern_key},
        )
    else:
        await po._append_audit(
            ctx,
            trace_id=trace,
            rule_id=ev.rule_name,
            outcome="ESCALATED",
            commands_run=(last_call.tool if last_call else ""),
            detail="\n".join(observations[-5:])[:2000],
            meta={
                "path": "fallback_react",
                "pattern_key": pattern_key,
                "reason": final_reason if escalated else "max_iterations_exhausted",
            },
        )
