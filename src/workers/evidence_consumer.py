"""Consume ``omni-diagnostic-evidence`` — read-only reasoning; emit SUGGEST_REMEDIATION to omni-actions."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pkg.rag.gate import evaluate_rag_gate
from pkg.reasoning import coerce_evidence_dict
from pkg.reasoning.evidence_signals import critical_evidence_present
from pkg.reasoning.evidence_anchor import llm_contradicts_sdk_facts, summarize_facts_for_anchor
from pkg.reasoning.reason_codes import (
    ERR_REA_LOG_SOURCE_UNAVAILABLE,
    ERR_REA_NO_PHYSICAL_PROOF,
    ERR_REA_SIGMA_GATE_BLOCKED,
    ERR_SEM_CHANNEL_MISMATCH,
    INV_NAMESPACE_ISOLATION,
    INV_NO_RESTART_ON_BROKEN_SPEC,
    INV_READ_BEFORE_MUTATE,
    PLANNER_PHASE_DONE,
)
from pkg.reasoning.sre_output import compact_sre_diagnosis
from pkg.reasoning.sanitize import (
    evidence_relevance_warning,
    filter_evidence_for_rag,
    format_batch_sanitized_analyst_user_text,
    format_sanitized_analyst_user_text,
)
from workers.alert_sdk_truth_compare import compare_alert_claim_to_sdk_state
from workers.analyst_agentic_loop import infer_blind_proof_lane_hint, run_agentic_mutate_plan
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
from workers.selflearning_shadow import run_shadow_selflearning
from workers.env_mode import namespace_allowed
from pkg.reasoning.diagnostic_policy import (
    build_reasoning_chain_payload,
    evaluate_diagnostic_invariants,
    evidence_suggests_broken_spec,
)
from pkg.reasoning.incident_matrix_profile import resolve_proof_lane
from workers.log_surge_probe import evaluate_log_surge_sigma_bypass, namespace_pod_from_batch
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
_RE_RULE_LINE = re.compile(r"(?:^|\n)\s*rule:\s*([^\n]+)", re.I)
_RE_SYMPTOM_LINE = re.compile(r"(?:^|\n)\s*symptom_group:\s*([^\n]+)", re.I)
def _f64(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _try_log_surge_sigma_bypass(
    ctx: WorkerHandlerContext,
    trace: str,
    batch: list[dict[str, Any]],
    rag_match_text: str | None,
) -> tuple[bool, dict[str, Any], bool]:
    """
    Optional Loki sustained-5xx path when sigma is false (API/Web + allowlist ns).
    Returns (bypass_ok, extra_meta, escalate_log_unavailable).
    """
    from pkg.reasoning.incident_matrix_profile import is_api_web_workload

    ws = ctx.settings
    if not bool(getattr(ws, "omni_sigma_log_bypass_enabled", False)):
        return False, {}, False
    ns, pod = namespace_pod_from_batch(batch)
    if not ns or not namespace_allowed(ws, ns):
        return False, {}, False
    if not is_api_web_workload(batch, rag_match_text=rag_match_text):
        return False, {}, False
    if not (pod or "").strip():
        return False, {}, False
    base = str(getattr(ws, "omni_loki_base_url", "") or "").strip()
    if not base:
        return False, {}, False
    res = await evaluate_log_surge_sigma_bypass(
        loki_base_url=base,
        namespace=ns,
        pod_name=pod,
        window_sec=int(getattr(ws, "omni_log_surge_window_sec", 300) or 300),
        min_lines=int(getattr(ws, "omni_log_surge_min_lines", 5) or 5),
        min_ratio=float(getattr(ws, "omni_log_surge_min_ratio", 0.5) or 0.5),
        line_limit=int(getattr(ws, "omni_log_surge_line_limit", 500) or 500),
        timeout_sec=float(getattr(ws, "omni_log_surge_http_timeout_sec", 25.0) or 25.0),
    )
    extra = dict(res.meta or {})
    extra["log_surge_reason"] = res.reason
    if res.ok:
        logger.info(
            "event=log_surge_sigma_bypass_ok trace=%s reason=%s lines=%s",
            trace,
            res.reason,
            extra.get("lines_fetched"),
        )
        return True, {"log_surge_bypass": True, **extra}, False
    if res.escalate_log_unavailable:
        return False, extra, True
    return False, extra, False


async def _proof_of_fault_gate(
    ctx: WorkerHandlerContext,
    *,
    trace: str,
    batch: list[dict[str, Any]],
    rag_match_text: str | None = None,
    blind_lane_hint: str | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    critical = critical_evidence_present(batch)
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
    lane, lane_src = resolve_proof_lane(
        batch, rag_match_text=rag_match_text, blind_lane_hint=blind_lane_hint
    )
    meta: dict[str, Any] = {
        "critical_evidence": critical,
        "sigma_ok": sigma_ok,
        "window_needed": needed,
        "baseline": {"dr": dr, "z_cpu": z_cpu, "z_mem": z_mem, "threshold": z_thr},
        "proof_lane": lane,
        "proof_lane_source": lane_src,
    }
    if not critical:
        return False, ERR_REA_NO_PHYSICAL_PROOF, meta

    legacy = not bool(getattr(ctx.settings, "omni_proof_lane_enabled", True))
    if legacy:
        if not sigma_ok:
            by_ok, extra, esc = await _try_log_surge_sigma_bypass(ctx, trace, batch, rag_match_text)
            if by_ok:
                meta.update(extra)
                meta["sigma_ok"] = True
                meta["sigma_bypass_via_log_surge"] = True
                return True, "", meta
            if esc:
                meta.update(extra)
                return False, ERR_REA_LOG_SOURCE_UNAVAILABLE, meta
            return False, ERR_REA_SIGMA_GATE_BLOCKED, meta

        if critical and sigma_ok:
            cur = int(await ctx.redis.incr(wkey))
            await ctx.redis.expire(wkey, 600)
        else:
            await ctx.redis.delete(wkey)
            cur = 0
        window_ok = cur >= needed
        meta["window_count"] = cur
        meta["sigma_ok"] = sigma_ok
        if not window_ok:
            return False, ERR_REA_SIGMA_GATE_BLOCKED, meta
        return True, "", meta

    if lane == "state":
        meta["sigma_ok"] = True
        meta["sigma_bypass_reason"] = "state_lane_physical_proof"
        needed_eff = 1
        cur = int(await ctx.redis.incr(wkey))
        await ctx.redis.expire(wkey, 600)
        meta["window_count"] = cur
        if cur < needed_eff:
            return False, ERR_REA_SIGMA_GATE_BLOCKED, meta
        return True, "", meta

    if lane == "resource":
        if not sigma_ok:
            return False, ERR_REA_SIGMA_GATE_BLOCKED, meta
        cur = int(await ctx.redis.incr(wkey))
        await ctx.redis.expire(wkey, 600)
        meta["window_count"] = cur
        meta["sigma_ok"] = sigma_ok
        if cur < needed:
            return False, ERR_REA_SIGMA_GATE_BLOCKED, meta
        return True, "", meta

    # app_log
    if sigma_ok:
        cur = int(await ctx.redis.incr(wkey))
        await ctx.redis.expire(wkey, 600)
        meta["window_count"] = cur
        meta["sigma_ok"] = sigma_ok
        if cur < needed:
            return False, ERR_REA_SIGMA_GATE_BLOCKED, meta
        return True, "", meta

    by_ok, extra, esc = await _try_log_surge_sigma_bypass(ctx, trace, batch, rag_match_text)
    if by_ok:
        meta.update(extra)
        meta["sigma_ok"] = True
        meta["sigma_bypass_via_log_surge"] = True
        return True, "", meta
    if esc:
        meta.update(extra)
        return False, ERR_REA_LOG_SOURCE_UNAVAILABLE, meta
    return False, ERR_REA_SIGMA_GATE_BLOCKED, meta


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
    rm = _RE_RULE_LINE.search(t)
    if rm:
        rule = rm.group(1).strip()
        if rule and rule != "n/a":
            h.setdefault("alertname", rule[:240])
    sm = _RE_SYMPTOM_LINE.search(t)
    if sm:
        sg = sm.group(1).strip()
        if sg:
            h.setdefault("symptom_group", sg[:240])
    return h if h else None


def _hints_from_evidence_batch(batch: list[dict[str, Any]], text: str) -> dict[str, str] | None:
    """Merge structured hints from evidence dicts + sanitized analyst text."""
    from pkg.reasoning.incident_matrix_profile import pick_matrix_row_for_batch

    h: dict[str, str] = dict(_hints_from_evidence_text(text) or {})
    if batch:
        ar = str(batch[0].get("alert_rule") or "").strip()
        if ar:
            h.setdefault("alertname", ar[:240])
        sg = str(batch[0].get("symptom_group") or "").strip()
        if sg:
            h.setdefault("symptom_group", sg[:240])
        row = pick_matrix_row_for_batch(batch, rag_match_text=None)
        if row:
            dp = row.get("diagnostic_pattern")
            if isinstance(dp, str) and dp.strip():
                h.setdefault("diagnostic_pattern", dp.strip()[:240])
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
    verdict: str | None = None,
    lane: str | None = None,
    thought_process: list[str] | None = None,
    invariant_id: str | None = None,
    reasoning_chain: dict[str, Any] | None = None,
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
        verdict=verdict,
        lane=lane,
        thought_process=thought_process,
        invariant_id=invariant_id,
        reasoning_chain=reasoning_chain,
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
    rag_match_text: str | None = None,
    rag_reasoning_hints: str | None = None,
) -> None:
    """
    Planner-first mutate emission:
    - optional blind proof_lane hint (matrix miss) before planner
    - always ask LLM planner (max N steps) using Fact Table + sanitized context
    - proof_of_fault gate (with blind lane hint)
    - diagnostic invariant gate (INV_*) before EXECUTE_MUTATE
    """
    blind_pre = await infer_blind_proof_lane_hint(
        ctx, batch, sanitized_text=sanitized_text, rag_match_text=rag_match_text
    )
    lane_for_mx, _ls = resolve_proof_lane(batch, rag_match_text=rag_match_text)
    mx = int(getattr(ctx.settings, "autonomous_agentic_max_steps", 5) or 5)
    if lane_for_mx == "state":
        mx = max(mx, 8)
    plan = await run_agentic_mutate_plan(
        ctx,
        trace=trace,
        sanitized_text=sanitized_text,
        batch=batch,
        max_steps=mx,
        rag_reasoning_hints=rag_reasoning_hints,
    )
    discovery_steps: list[str] = list(plan.get("discovery_steps") or []) if plan else []
    if plan and str(plan.get("reason_code") or "") == PLANNER_PHASE_DONE:
        fa = str(plan.get("final_analysis") or "").strip()
        rc = plan.get("reasoning_chain") if isinstance(plan.get("reasoning_chain"), dict) else None
        tp_done: list[str] = []
        if rc and isinstance(rc.get("thought_process"), list):
            tp_done = [str(x) for x in rc["thought_process"]][:32]
        lane_g = str(rc.get("lane") or "state") if isinstance(rc, dict) else "state"
        await _emit_suggest_remediation(
            ctx,
            trace=trace,
            diagnosis=fa or "Planner concluded diagnostic session; see reasoning_chain.",
            confidence=0.78,
            source="PLANNER_DIAGNOSTIC_DONE",
            suggested_tool="k8s_describe_resource",
            reasoning_chain=rc,
            verdict="SUGGEST_FIX",
            lane=lane_g,
            thought_process=tp_done,
        )
        await emit_transition(
            ctx,
            trace_id=trace,
            transition=TRANSITION_PLAN_EMITTED,
            component="evidence_consumer",
            detail="planner_phase_done_suggest",
            meta={"phase": "done", "reason_code": PLANNER_PHASE_DONE},
        )
        logger.info("event=planner_phase_done_emitted trace=%s", trace)
        return
    if plan and str(plan.get("reason_code") or "") == ERR_SEM_CHANNEL_MISMATCH:
        suggested = str(plan.get("suggested_tool") or "").strip() or "inspect_pod_details"
        rc = plan.get("reasoning_chain") if isinstance(plan.get("reasoning_chain"), dict) else None
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
            reasoning_chain=rc,
        )
        await emit_transition(
            ctx,
            trace_id=trace,
            transition=TRANSITION_PLAN_EMITTED,
            component="evidence_consumer",
            detail=f"planner_readonly_routed:{suggested}",
            meta={"reason_code": ERR_SEM_CHANNEL_MISMATCH},
        )
        # Continue: synthesize rollout restart from evidence so proof-of-fault + INV_* gates still run
        # (hard-fail / broken-spec path must reach evaluate_diagnostic_invariants).
        lane_saved = plan.get("lane_hint") if isinstance(plan.get("lane_hint"), str) else None
        rr = rollout_args_from_evidence_batch(batch)
        is_fault = workload_fault_incident_rollout_eligible(batch)
        is_cpu = workload_cpu_incident_rollout_eligible(batch)
        if not rr or not (is_fault or is_cpu):
            return
        plan = {
            "tool_name": "k8s_rollout_restart",
            "args": dict(rr),
            "discovery_steps": discovery_steps,
            "lane_hint": lane_saved.strip() if lane_saved and lane_saved.strip() else None,
            "reasoning_chain": rc,
        }
        logger.warning(
            "event=agentic_mutate_fallback_after_readonly_mismatch trace=%s tool=k8s_rollout_restart",
            trace,
        )
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
    blind_lane_eff: str | None = blind_pre
    if plan:
        lh_raw = plan.get("lane_hint")
        if isinstance(lh_raw, str) and lh_raw.strip():
            blind_lane_eff = lh_raw.strip()
    proof_ok, reason_code, proof_meta = await _proof_of_fault_gate(
        ctx,
        trace=trace,
        batch=batch,
        rag_match_text=rag_match_text,
        blind_lane_hint=blind_lane_eff,
    )
    if not proof_ok:
        if reason_code == ERR_REA_LOG_SOURCE_UNAVAILABLE:
            await emit_telegram_escalation(
                ctx,
                trace,
                "Sigma blocked & Log source unavailable",
                reason="SIGMA_LOG_UNAVAILABLE",
            )
            await emit_terminal_tombstone(
                ctx,
                trace_id=trace,
                reason_code=ERR_REA_LOG_SOURCE_UNAVAILABLE,
                component="evidence_consumer",
                detail="Sigma blocked & Log source unavailable",
                meta=proof_meta,
            )
            return
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
    pl_inv = proof_meta.get("proof_lane")
    proof_lane_for_inv = str(pl_inv).strip() if isinstance(pl_inv, str) and pl_inv.strip() else None
    inv_ok, inv_reason, inv_meta = evaluate_diagnostic_invariants(
        ctx.settings,
        tool_name=tn,
        args=args,
        batch=batch,
        discovery_tool_names=discovery_steps,
        proof_lane=proof_lane_for_inv,
    )
    if not inv_ok:
        lane_guess = str(proof_meta.get("proof_lane") or "unknown")
        tp: list[str] = []
        if plan and isinstance(plan.get("reasoning_chain"), dict):
            raw_tp = plan["reasoning_chain"].get("thought_process")
            if isinstance(raw_tp, list):
                tp = [str(x) for x in raw_tp][:24]
        tp.append(f"Invariant blocked mutate: {inv_reason}")
        verdict = (
            "SUGGEST_FIX_SOURCE"
            if inv_reason == INV_NO_RESTART_ON_BROKEN_SPEC
            else "DEFERRED"
        )
        rc = build_reasoning_chain_payload(
            verdict=verdict,
            lane=lane_guess,
            thought_process=tp,
            invariant_id=inv_reason,
        )
        await _emit_suggest_remediation(
            ctx,
            trace=trace,
            diagnosis=(
                f"Diagnostic policy blocked EXECUTE_MUTATE ({inv_reason}). "
                "See reasoning_chain; fix source-of-truth or add read-only discovery."
            ),
            confidence=0.55,
            source="DIAGNOSTIC_INVARIANT_GATE",
            suggested_tool="k8s_describe_resource",
            reasoning_chain=rc,
            verdict=verdict,
            lane=lane_guess,
            thought_process=tp,
            invariant_id=inv_reason,
        )
        if inv_meta.get("security_signal") or inv_reason == INV_NAMESPACE_ISOLATION:
            await emit_telegram_escalation(
                ctx,
                trace,
                f"invariant={inv_reason} tool={tn} args_namespace={args.get('namespace')!r}",
                reason=str(inv_reason),
            )
        await emit_transition(
            ctx,
            trace_id=trace,
            transition=TRANSITION_PLAN_EMITTED,
            component="evidence_consumer",
            detail=f"diagnostic_invariant_blocked:{inv_reason}",
            meta={"invariant_id": inv_reason, "inv_meta": inv_meta},
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
    exec_rc = plan.get("reasoning_chain") if isinstance(plan, dict) else None
    await emit_execute_mutate(
        ctx,
        trace=trace,
        tool_name=tn,
        args=args,
        attempt_count=1,
        reasoning_chain=exec_rc if isinstance(exec_rc, dict) else None,
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

        ev_hints = _hints_from_evidence_batch(batch, sanitized_text)
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
            rag_txt = (gate_out.match_text_en or gate_out.formatted or "").strip() or None
            proof_lane_pre, lane_src = resolve_proof_lane(batch, rag_match_text=rag_txt)
            broken = evidence_suggests_broken_spec(batch)
            intercept_rag_suggest = (proof_lane_pre == "state") or broken

            if intercept_rag_suggest:
                hints_body = (
                    "(RAG reference for planner — verify with read-only tools; not ground truth)\n\n"
                    f"{gate_out.formatted.strip()[:12000]}\n\n"
                    f"chunk_ids: {getattr(gate_out, 'chunk_ids', None) or []}\n"
                    f"suggested_tool_hint: {gate_out.suggested_tool or 'kubectl_describe_pod'}\n"
                )
                logger.info(
                    "event=rag_hints_buffered trace=%s proof_lane=%s lane_src=%s broken_spec=%s",
                    trace,
                    proof_lane_pre,
                    lane_src,
                    broken,
                )
                await store_autonomous_trace_context(ctx.redis, trace, batch=batch, sanitized_text=sanitized_text)
                await emit_transition(
                    ctx,
                    trace_id=trace,
                    transition=TRANSITION_PLAN_EMITTED,
                    component="evidence_consumer",
                    detail="rag_hints_only_await_planner",
                    meta={"proof_lane": proof_lane_pre, "intercept_rag_suggest": True},
                )
                await _emit_agentic_mutate_if_any(
                    ctx,
                    trace,
                    batch,
                    sanitized_text=sanitized_text,
                    rag_match_text=rag_txt,
                    rag_reasoning_hints=hints_body,
                )
                return f"[trace={trace}] RAG hints absorbed into planner (state/broken-spec intercept)."

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
            await _emit_agentic_mutate_if_any(
                ctx, trace, batch, sanitized_text=sanitized_text, rag_match_text=rag_txt
            )
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
            await run_shadow_selflearning(
                ctx,
                trace=trace,
                sanitized_text=sanitized_text,
                machine=machine,
            )

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
