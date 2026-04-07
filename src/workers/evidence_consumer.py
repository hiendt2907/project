"""Consume ``omni-diagnostic-evidence`` — read-only reasoning; emit SUGGEST_REMEDIATION to omni-actions."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pkg.rag.gate import evaluate_rag_gate
from pkg.reasoning import coerce_evidence_dict
from pkg.reasoning.evidence_anchor import llm_contradicts_sdk_facts, summarize_facts_for_anchor
from pkg.reasoning.reason_codes import (
    ERR_REA_NO_PHYSICAL_PROOF,
    ERR_REA_SIGMA_GATE_BLOCKED,
    ERR_SEM_CHANNEL_MISMATCH,
)
from pkg.reasoning.sre_output import compact_sre_diagnosis
from pkg.reasoning.sanitize import (
    evidence_relevance_warning,
    filter_evidence_for_rag,
    format_batch_sanitized_analyst_user_text,
    format_sanitized_analyst_user_text,
)
from workers.alert_sdk_truth_compare import compare_alert_claim_to_sdk_state
from workers.analyst_agentic_loop import run_agentic_mutate_plan
from workers.evidence_batch import append_evidence_and_take_flush_batch
from workers.evidence_mutate_emit import (
    emit_execute_mutate,
    rollout_args_from_evidence_batch,
    workload_cpu_incident_rollout_eligible,
    workload_fault_incident_rollout_eligible,
    store_autonomous_trace_context,
)
from workers.handler_context import WorkerHandlerContext
from workers.baseline_snapshot import REDIS_KEY_SNAPSHOT
from workers.autonomous_execute import MUTATE_TOOL_ALLOWLIST
from workers.k8s_tools import deployment_evidence_snapshot
from workers.llm_context_budget import effective_reply_max_words
from workers.omni_actions_remediation import build_suggest_remediation_body
from workers.reasoning_evidence_inbound import (
    reason_diagnostic_evidence_only,
    reason_diagnostic_rag_miss_sdk_only,
)
from workers.telegram_escalation import emit_telegram_escalation
from workers.request_trace import pop_trace_id, push_trace_id
from workers.telegram_outbound import send_telegram_out_for_inbound
from workers import ollama_prompts_en as ope
from workers.metrics_exporter import inc_evidence_llm_contradiction
from workers.autonomy_contract import (
    TRANSITION_CONTEXT_READY,
    TRANSITION_DIAGNOSED,
    TRANSITION_PLAN_EMITTED,
    emit_terminal_tombstone,
    emit_transition,
)

logger = logging.getLogger(__name__)


def _rag_search_failed(detail: Any) -> bool:
    """RAG/embed/pgvector failed (400/500) — use fact-only SDK reasoning."""
    return isinstance(detail, dict) and str(detail.get("reason") or "") == "search_error"


def build_sdk_fact_only_prompt(batch: list[dict[str, Any]]) -> str:
    """Compact SDK facts for LLM when RAG is unavailable (no raw log dumps)."""
    if not batch:
        return "(no evidence batch)"
    lines: list[str] = []
    ar = str(batch[0].get("alert_rule") or "").strip()[:240]
    ah = str(batch[0].get("alert_hint") or "").strip()[:500]
    lines.append(f"error_reason_hint: alert_rule={ar} alert_hint={ah}")
    for b in batch:
        probe = str(b.get("probe") or "?")
        ef_raw = b.get("extracted_fact")
        if isinstance(ef_raw, dict):
            blob = json.dumps(ef_raw, ensure_ascii=False)[:3500]
        elif isinstance(ef_raw, str) and ef_raw.strip().startswith("{"):
            try:
                blob = json.dumps(json.loads(ef_raw), ensure_ascii=False)[:3500]
            except Exception:
                blob = ef_raw[:3500]
        else:
            blob = str(ef_raw or "")[:3500]
        lines.append(f"[{probe}] extracted_fact={blob}")
    return "\n".join(lines)[:24000]


_NS_POD = re.compile(
    r"\bnamespace[=:]\s*([\w.-]+)|\bns[=:]\s*([\w.-]+)|\bpod[=:]\s*([\w.-]+)",
    re.I,
)
_RE_CRITICAL_FAULT = re.compile(
    r"(crashloop|createcontainer|imagepull|oomkilled|oom|failedmount|unschedul|readiness.*fail|liveness.*fail|waiting|backoff|exit[_\s-]*code)",
    re.IGNORECASE,
)


def _f64(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _critical_evidence_present(batch: list[dict[str, Any]]) -> bool:
    for b in batch:
        hint = str(b.get("alert_hint") or "")
        if _RE_CRITICAL_FAULT.search(hint):
            return True
        snip = str(b.get("canonical_query_snippet") or "").strip()
        if not snip.startswith("{"):
            continue
        try:
            j = json.loads(snip)
        except Exception:
            continue
        if not isinstance(j, dict):
            continue
        labels = j.get("labels")
        if not isinstance(labels, dict):
            continue
        reason = str(labels.get("reason") or "")
        alertname = str(labels.get("alertname") or "")
        if _RE_CRITICAL_FAULT.search(reason) or _RE_CRITICAL_FAULT.search(alertname):
            return True
    return False


async def _proof_of_fault_gate(
    ctx: WorkerHandlerContext,
    *,
    trace: str,
    batch: list[dict[str, Any]],
) -> tuple[bool, str, dict[str, Any]]:
    critical = _critical_evidence_present(batch)
    snap_raw = await ctx.redis.get(REDIS_KEY_SNAPSHOT)
    snap: dict[str, Any] = {}
    if snap_raw:
        try:
            snap = json.loads(snap_raw.decode() if isinstance(snap_raw, bytes) else snap_raw)
        except Exception:
            snap = {}
    z_thr = float(getattr(ctx.settings, "baseline_dr_z_threshold", 3.0) or 3.0)
    dr = bool(snap.get("dr"))
    z_cpu = _f64(snap.get("z_cpu"))
    z_mem = _f64(snap.get("z_mem"))
    z_hit = bool((z_cpu is not None and abs(z_cpu) >= z_thr) or (z_mem is not None and abs(z_mem) >= z_thr))
    sigma_ok = bool(dr or z_hit)

    needed = max(1, int(getattr(ctx.settings, "autonomous_sigma_observation_window", 1) or 1))
    wkey = f"omni:proof_of_fault:window:{trace}"
    if critical and sigma_ok:
        cur = int(await ctx.redis.incr(wkey))
        await ctx.redis.expire(wkey, 600)
    else:
        await ctx.redis.delete(wkey)
        cur = 0
    window_ok = cur >= needed
    meta = {
        "critical_evidence": critical,
        "sigma_ok": sigma_ok,
        "window_count": cur,
        "window_needed": needed,
        "baseline": {"dr": dr, "z_cpu": z_cpu, "z_mem": z_mem, "threshold": z_thr},
    }
    if not critical:
        return False, ERR_REA_NO_PHYSICAL_PROOF, meta
    if not sigma_ok or not window_ok:
        return False, ERR_REA_SIGMA_GATE_BLOCKED, meta
    return True, "", meta


def _hints_from_evidence_text(text: str) -> dict[str, str] | None:
    """Best-effort namespace/pod from sanitized text for RagGate GIGO."""
    t = (text or "")[:12000]
    h: dict[str, str] = {}
    for m in _NS_POD.finditer(t):
        g = [x for x in m.groups() if x]
        if not g:
            continue
        val = g[0].strip()
        if not val:
            continue
        frag = m.group(0).lower()
        if "pod" in frag and "namespace" not in frag and "ns" not in frag:
            h.setdefault("pod_name", val)
        else:
            h.setdefault("namespace", val)
    return h if h else None


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


async def _emit_suggest_remediation(
    ctx: WorkerHandlerContext,
    *,
    trace: str,
    diagnosis: str,
    confidence: float,
    source: str,
    suggested_tool: str,
) -> None:
    if not ctx.settings.trace_correlation_ping_enabled:
        return
    k = ctx.kafka
    if k is None:
        return
    tid = str(trace or "").strip()
    if not tid:
        return
    body = build_suggest_remediation_body(
        tid,
        diagnosis=diagnosis,
        confidence=_clamp01(confidence),
        source=source,
        suggested_tool=suggested_tool,
    )
    try:
        await k.send_dict(ctx.settings.kafka_topic_actions, {"data": json.dumps(body, ensure_ascii=False)})
        logger.info(
            "event=action_emitted action=SUGGEST_REMEDIATION trace=%s source=%s",
            tid,
            source,
        )
    except Exception as e:
        logger.warning("action_emit skip: %s", e)


async def _emit_agentic_mutate_if_any(
    ctx: WorkerHandlerContext,
    trace: str,
    batch: list[dict[str, Any]],
    *,
    sanitized_text: str,
) -> None:
    """
    Planner-first mutate emission:
    - always ask LLM planner (max N steps) using sanitized SDK/RAG context
    - no heuristic auto-rollout shortcut
    """
    mx = int(getattr(ctx.settings, "autonomous_agentic_max_steps", 5) or 5)
    plan = await run_agentic_mutate_plan(
        ctx,
        trace=trace,
        sanitized_text=sanitized_text,
        batch=batch,
        max_steps=mx,
    )
    if plan and str(plan.get("reason_code") or "") == ERR_SEM_CHANNEL_MISMATCH:
        suggested = str(plan.get("suggested_tool") or "").strip() or "inspect_pod_details"
        await _emit_suggest_remediation(
            ctx,
            trace=trace,
            diagnosis=(
                "Planner produced read-only tool for EXECUTE_MUTATE; route to suggestion channel. "
                f"reason_code={ERR_SEM_CHANNEL_MISMATCH}"
            ),
            confidence=0.5,
            source="PLANNER_READONLY_ROUTE",
            suggested_tool=suggested,
        )
        await emit_transition(
            ctx,
            trace_id=trace,
            transition=TRANSITION_PLAN_EMITTED,
            component="evidence_consumer",
            detail=f"planner_readonly_routed:{suggested}",
            meta={"reason_code": ERR_SEM_CHANNEL_MISMATCH},
        )
        return
    if not plan:
        # Planner-first failed (LLM unavailable/invalid JSON): safe deterministic fallback
        # only for clearly identified workload incidents.
        rr = rollout_args_from_evidence_batch(batch)
        if not rr:
            return
        is_fault = workload_fault_incident_rollout_eligible(batch)
        is_cpu = workload_cpu_incident_rollout_eligible(batch)
        if not (is_fault or is_cpu):
            return
        tn = "k8s_rollout_restart"
        args = dict(rr)
        logger.warning(
            "event=agentic_mutate_fallback trace=%s tool=%s reason=planner_unavailable cpu=%s fault=%s",
            trace,
            tn,
            is_cpu,
            is_fault,
        )
    else:
        tn = str(plan.get("tool_name") or "").strip()
        args = dict(plan.get("args") or {})
        if not tn:
            return
    if tn not in MUTATE_TOOL_ALLOWLIST:
        await _emit_suggest_remediation(
            ctx,
            trace=trace,
            diagnosis=(
                f"Planner proposed non-mutating/unregistered tool '{tn}'. "
                f"reason_code={ERR_SEM_CHANNEL_MISMATCH}"
            ),
            confidence=0.35,
            source="PLANNER_TOOL_REJECTED",
            suggested_tool=tn or "inspect_pod_details",
        )
        await emit_transition(
            ctx,
            trace_id=trace,
            transition=TRANSITION_PLAN_EMITTED,
            component="evidence_consumer",
            detail=f"planner_tool_rejected:{tn or 'unknown'}",
            meta={"reason_code": ERR_SEM_CHANNEL_MISMATCH},
        )
        return
    proof_ok, reason_code, proof_meta = await _proof_of_fault_gate(ctx, trace=trace, batch=batch)
    if not proof_ok:
        await _emit_suggest_remediation(
            ctx,
            trace=trace,
            diagnosis=f"Mutate blocked by evidence gate. reason_code={reason_code}",
            confidence=0.4,
            source="PROOF_OF_FAULT_GATE",
            suggested_tool="inspect_pod_details",
        )
        await emit_transition(
            ctx,
            trace_id=trace,
            transition=TRANSITION_PLAN_EMITTED,
            component="evidence_consumer",
            detail=f"proof_gate_blocked:{reason_code}",
            meta={"reason_code": reason_code, "proof_of_fault": proof_meta},
        )
        return
    if tn == "k8s_rollout_restart":
        ns = str(args.get("namespace") or "").strip()
        dep = str(args.get("deployment") or "").strip()
        if ns and dep:
            try:
                args["evidence_snapshot"] = await deployment_evidence_snapshot(ns, dep)
            except Exception:
                args["evidence_snapshot"] = {}
    args["proof_of_fault"] = proof_meta
    await emit_execute_mutate(
        ctx,
        trace=trace,
        tool_name=tn,
        args=args,
        attempt_count=1,
    )


async def reason_from_diagnostic_evidence(ctx: WorkerHandlerContext, fields: dict[str, str]) -> str:
    """Evidence → batch → so alert vs state machine SDK (nếu mâu thuẫn rõ) → RagGate | LLM."""
    raw = fields.get("data") or "{}"
    try:
        ev_doc = json.loads(raw)
    except Exception:
        ev_doc = {"kind": "parse_error", "raw": raw[:8000]}
    ev_doc = coerce_evidence_dict(ev_doc)
    trace = str(ev_doc.get("trace_id") or "evidence-unknown")
    tok = push_trace_id(trace)
    try:
        ctx.inbound_trace_id = trace
        await emit_transition(
            ctx,
            trace_id=trace,
            transition=TRANSITION_CONTEXT_READY,
            component="evidence_consumer",
            detail="diagnostic_evidence_received",
        )
        rel = evidence_relevance_warning(
            str(ev_doc.get("alert_hint") or ""),
            str(ev_doc.get("probe") or ""),
        )
        if rel:
            logger.warning("event=evidence_relevance_mismatch detail=%s", rel[:500])

        batch = await append_evidence_and_take_flush_batch(ctx.redis, trace, ev_doc)
        if batch is None:
            return ""
        await emit_transition(
            ctx,
            trace_id=trace,
            transition=TRANSITION_DIAGNOSED,
            component="evidence_consumer",
            detail="evidence_batch_ready",
            meta={"batch_size": len(batch)},
        )

        logger.info(
            "event=diag_batch_flush trace=%s probes=%s",
            trace,
            [x.get("probe") for x in batch],
        )

        chat_id: int | None = None
        ctx_blob = await ctx.redis.get(f"omni:evidence_reply:{trace}")
        if ctx_blob:
            try:
                meta = json.loads(ctx_blob.decode() if isinstance(ctx_blob, bytes) else ctx_blob)
                cid = meta.get("chat_id")
                if cid is not None:
                    chat_id = int(cid)
            except Exception:
                logger.warning("evidence_reply context parse failed")

        by_probe = {str(b.get("probe") or ""): dict(b) for b in batch}
        contrast = compare_alert_claim_to_sdk_state(by_probe)
        if contrast is not None:
            await _emit_suggest_remediation(
                ctx,
                trace=trace,
                diagnosis=contrast.strip(),
                confidence=0.95,
                source="STATE_MACHINE_CONTRAST",
                suggested_tool="verify_metrics_alignment",
            )
            if chat_id is not None:
                pld = {
                    "trace_id": trace,
                    "source": "diagnostic_evidence",
                    "text": contrast,
                    "diagnostic_evidence_sanitized": True,
                }
                await send_telegram_out_for_inbound(ctx, pld, trace, contrast)
            await emit_transition(
                ctx,
                trace_id=trace,
                transition=TRANSITION_PLAN_EMITTED,
                component="evidence_consumer",
                detail="state_machine_contrast_suggested",
            )
            return contrast

        sanitized_text = format_batch_sanitized_analyst_user_text(batch)
        if len(batch) == 1:
            sanitized_text = format_sanitized_analyst_user_text(batch[0])

        ev_hints = _hints_from_evidence_text(sanitized_text)
        rag_query = filter_evidence_for_rag(batch)
        gate_out = await evaluate_rag_gate(ctx, rag_query, hints=ev_hints, trace=trace)
        rag_gate_failed = bool(
            not gate_out.hit and _rag_search_failed(getattr(gate_out, "detail", None))
        )
        analyst_text = sanitized_text
        if rag_gate_failed:
            logger.warning(
                "event=rag_fallback_sdk_only trace=%s detail=%s",
                trace,
                getattr(gate_out, "detail", None),
            )
            analyst_text = (
                "WARNING: DIAGNOSIS_WITHOUT_RAG_KNOWLEDGE\n\n" + build_sdk_fact_only_prompt(batch)
            )

        if gate_out.hit and (gate_out.formatted or "").strip():
            logger.info(
                "event=rag_truth_citations trace=%s chunk_ids=%s best_score=%s",
                trace,
                getattr(gate_out, "chunk_ids", None) or [],
                gate_out.best_score,
            )
            diag_en = (gate_out.match_text_en or "").strip() or gate_out.formatted.strip()
            mw = effective_reply_max_words(ctx.settings)
            raw_fmt = gate_out.formatted.strip()
            out = compact_sre_diagnosis(
                ope.truncate_plain_text_to_max_words(raw_fmt, max_words=mw),
                max_words=mw,
            )
            await _emit_suggest_remediation(
                ctx,
                trace=trace,
                diagnosis=diag_en,
                confidence=gate_out.best_score or 0.0,
                source="RAG_HIT",
                suggested_tool=gate_out.suggested_tool or "kubectl_describe_pod",
            )
            await store_autonomous_trace_context(ctx.redis, trace, batch=batch, sanitized_text=sanitized_text)
            await emit_transition(
                ctx,
                trace_id=trace,
                transition=TRANSITION_PLAN_EMITTED,
                component="evidence_consumer",
                detail="rag_hit_suggested",
            )
            await _emit_agentic_mutate_if_any(ctx, trace, batch, sanitized_text=sanitized_text)
            if chat_id is not None:
                pld = {
                    "trace_id": trace,
                    "source": "diagnostic_evidence",
                    "text": sanitized_text,
                    "diagnostic_evidence_sanitized": True,
                }
                await send_telegram_out_for_inbound(ctx, pld, trace, out)
            return out

        if bool(getattr(ctx.settings, "rag_truth_law_enforced", True)):
            # Gap A: RAG miss — do not stop; SDK-only LLM with two-channel contract.
            sdk_payload: dict[str, Any] = {
                "trace_id": trace,
                "source": "diagnostic_evidence",
                "text": analyst_text,
                "diagnostic_evidence_sanitized": True,
            }
            if chat_id is not None:
                sdk_payload["chat_id"] = chat_id
            sdk_out = await reason_diagnostic_rag_miss_sdk_only(ctx, sdk_payload, trace)
            human = str(sdk_out.get("human") or "").strip()
            machine = sdk_out.get("machine")
            raw_llm = str(sdk_out.get("raw_llm") or "")
            display_out = str(sdk_out.get("display_line") or human)
            if rag_gate_failed:
                display_out = f"{display_out.strip()}\n[SOURCE: SDK_FACTS_ONLY]"
            if not isinstance(machine, dict):
                machine = {}

            contradict_sdk = bool(
                getattr(ctx.settings, "rag_evidence_contradiction_check_enabled", True)
            ) and llm_contradicts_sdk_facts(human + "\n" + json.dumps(machine), summarize_facts_for_anchor(batch))
            if contradict_sdk:
                inc_evidence_llm_contradiction()
                await emit_telegram_escalation(
                    ctx,
                    trace,
                    f"contradiction blocked\nhuman={human}\nmachine={machine}",
                    reason="SDK_CONTRADICTION",
                )
                human = (
                    "CONTRADICTION_BLOCKED: model disagreed with SDK evidence. "
                    "ESCALATE for manual review."
                )
                machine = {"verdict": "ESCALATE", "hypothesis": "contradiction", "action": {}}

            verdict = str(machine.get("verdict") or "").upper()
            if verdict == "ESCALATE" or "ESCALATE" in human.upper():
                await emit_telegram_escalation(
                    ctx,
                    trace,
                    f"human={human}\nmachine={json.dumps(machine)}\nraw={raw_llm[:2000]}",
                    reason="RAG_MISS_SDK_ESCALATE",
                )
                await _emit_suggest_remediation(
                    ctx,
                    trace=trace,
                    diagnosis=human[:2000],
                    confidence=0.0,
                    source="SDK_FACTS_ONLY_ESCALATE" if rag_gate_failed else "SDK_ONLY_ESCALATE",
                    suggested_tool="escalate_to_human",
                )
                if chat_id is not None:
                    pld = {
                        "trace_id": trace,
                        "source": "diagnostic_evidence",
                        "text": sanitized_text,
                        "diagnostic_evidence_sanitized": True,
                    }
                    await send_telegram_out_for_inbound(ctx, pld, trace, human)
                await _emit_agentic_mutate_if_any(ctx, trace, batch, sanitized_text=sanitized_text)
                await emit_terminal_tombstone(
                    ctx,
                    trace_id=trace,
                    reason_code="SDK_ESCALATE",
                    component="evidence_consumer",
                    detail=human[:1200],
                )
                return display_out

            hyp = str(machine.get("hypothesis") or "")
            action = machine.get("action") if isinstance(machine.get("action"), dict) else {}
            tool = str((action or {}).get("tool") or "").strip()

            await _emit_suggest_remediation(
                ctx,
                trace=trace,
                diagnosis=f"{human}\n[{hyp}]"[:4000],
                confidence=0.55,
                source="SDK_FACTS_ONLY" if rag_gate_failed else "SDK_ONLY_DIAGNOSE",
                suggested_tool=tool or "inspect_pod_logs",
            )
            await store_autonomous_trace_context(ctx.redis, trace, batch=batch, sanitized_text=sanitized_text)
            await emit_transition(
                ctx,
                trace_id=trace,
                transition=TRANSITION_PLAN_EMITTED,
                component="evidence_consumer",
                detail=f"sdk_only_plan:{tool or 'none'}",
            )

            await _emit_agentic_mutate_if_any(ctx, trace, batch, sanitized_text=sanitized_text)

            if chat_id is not None:
                pld = {
                    "trace_id": trace,
                    "source": "diagnostic_evidence",
                    "text": sanitized_text,
                    "diagnostic_evidence_sanitized": True,
                }
                await send_telegram_out_for_inbound(ctx, pld, trace, human)
            return display_out

        payload: dict[str, Any] = {
            "trace_id": trace,
            "source": "diagnostic_evidence",
            "text": sanitized_text,
            "diagnostic_evidence_sanitized": True,
            "batched_probes": [str(b.get("probe") or "") for b in batch],
            "rag_gate_evaluated": True,
            "batched_evidence_docs": batch,
        }
        if chat_id is not None:
            payload["chat_id"] = chat_id
        out = await reason_diagnostic_evidence_only(ctx, payload, trace)
        anchor_on = bool(getattr(ctx.settings, "rag_evidence_contradiction_check_enabled", True))
        contradict = anchor_on and llm_contradicts_sdk_facts(out, summarize_facts_for_anchor(batch))
        if contradict:
            inc_evidence_llm_contradiction()
            logger.error(
                "event=evidence_llm_contradiction trace=%s — replacing output",
                trace,
            )
            out = (
                "CONTRADICTION_BLOCKED: model output disagreed with SDK evidence. "
                "I_DO_NOT_KNOW_PROCEED_TO_MANUAL"
            )
            await _emit_suggest_remediation(
                ctx,
                trace=trace,
                diagnosis=out,
                confidence=0.0,
                source="CONTRADICTION_BLOCKED",
                suggested_tool="reprobe_sdk",
            )
        else:
            await _emit_suggest_remediation(
                ctx,
                trace=trace,
                diagnosis=(out or "").strip() or "Empty analyst output.",
                confidence=0.72,
                source="LLM_ANALYST",
                suggested_tool="inspect_pod_logs",
            )
        if not contradict:
            await store_autonomous_trace_context(ctx.redis, trace, batch=batch, sanitized_text=sanitized_text)
            await emit_transition(
                ctx,
                trace_id=trace,
                transition=TRANSITION_PLAN_EMITTED,
                component="evidence_consumer",
                detail="llm_analyst_plan_ready",
            )
            await _emit_agentic_mutate_if_any(ctx, trace, batch, sanitized_text=sanitized_text)
        if chat_id is not None:
            await send_telegram_out_for_inbound(ctx, payload, trace, out)
        return out
    finally:
        pop_trace_id(tok)
