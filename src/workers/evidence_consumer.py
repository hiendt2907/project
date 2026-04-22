"""Consume ``omni-diagnostic-evidence`` — read-only reasoning; emit SUGGEST_REMEDIATION to omni-actions.

Planner (`run_agentic_mutate_plan`) may use InitialSymptom + sole-evaluator prompts; deterministic mutate,
proof-of-fault, and diagnostic policy gates below still apply and are not overridden by the LLM planner alone.
"""

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
from workers.memory.initial_symptom import initial_symptom_from_evidence_batch
from workers.memory.trace_memory import load_trace_memory
from workers.evidence_batch import append_evidence_and_take_flush_batch
from workers.evidence_mutate_emit import (
    emit_execute_mutate,
    emit_hitl_pending,
    _siem_alert_labels,
    _siem_hitl_required,
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
from workers.omni_actions_remediation import build_suggest_os_runbook_body, build_suggest_remediation_body
from workers.os_executor_adapter import wrap_host_command
from workers.schemas.agentic_planner import validate_suggest_os_runbook_data
from workers.reasoning_evidence_inbound import (
    reason_diagnostic_evidence_only,
    reason_diagnostic_rag_miss_sdk_only,
    _identity_from_batch,
    _build_identity_prefix,
)
from workers.archivist import build_strong_recall_prefix, recall_playbook_advisory
from services.playbook.store import PlaybookStore
from services.playbook.matcher import PlaybookMatcher
from workers.selflearning_shadow import run_shadow_selflearning
from workers.env_mode import namespace_allowed
from pkg.reasoning.diagnostic_policy import (
    build_reasoning_chain_payload,
    evaluate_diagnostic_invariants,
    evidence_suggests_broken_spec,
)
from pkg.reasoning.deterministic_mutate_from_evidence import (
    _evidence_suggests_credential_failure,
    chaos_credential_lab_autofix_plan_from_batch,
    deterministic_mutate_plan_from_batch,
    default_remediation_namespace,
    probe_driven_mutate_tools_for_settings,
)
from pkg.reasoning.incident_matrix_profile import alertname_from_batch, resolve_proof_lane
from pkg.reasoning.preflight_deployment_secret_refs import merge_preflight_deployment_secret_refs
from workers.log_surge_probe import evaluate_log_surge_sigma_bypass, namespace_pod_from_batch
from workers.telegram_escalation import emit_telegram_escalation, format_operator_action_card
from workers.request_trace import pop_trace_id, push_trace_id
from workers.telegram_outbound import send_telegram_out_for_inbound
from workers import llm_prompts_en as ope
from workers.metrics_exporter import inc_evidence_llm_contradiction
from workers.autonomy_contract import (
    TRANSITION_CONTEXT_READY,
    TRANSITION_DIAGNOSED,
    TRANSITION_PLAN_EMITTED,
    emit_terminal_tombstone,
    emit_transition,
)
from workers.tool_registry import get_tool_registry

logger = logging.getLogger(__name__)

# Self-remediation lab alerts: do not trust low-score RAG chunks as final diagnosis.
_SECURITY_SELF_REMEDIATION_ALERT_NAMES = frozenset(
    {
        "OmniRbacClusterAdminViolation",
        "OmniConfigMapGodModeProd",
        "OmniOomKilledPodNoRecovery",
    }
)
_RAG_SCORE_FLOOR_SECURITY = 0.85


def _shadow_os_mode(ctx: WorkerHandlerContext) -> bool:
    return bool(getattr(ctx.settings, "omni_shadow_os_mode", False))


def _derive_shadow_os_commands(
    *,
    tool_name: str,
    args: dict[str, Any],
    evidence_refs: list[str],
    trace: str,
) -> list[dict[str, Any]]:
    """Convert mutate intent into human-runnable Shadow OS command steps."""
    tn = str(tool_name or "").strip()
    if not tn:
        return []
    raw_payload = json.dumps({"tool_name": tn, "args": args}, ensure_ascii=False, sort_keys=True, default=str)
    inspect_cmd = wrap_host_command(
        "kubectl get pods -A --field-selector=status.phase!=Running | head -n 30"
    ).command
    apply_cmd = wrap_host_command(f"echo {json.dumps(raw_payload)} > /tmp/omni-shadow-{trace[:24]}.json").command
    rollback_cmd = wrap_host_command(f"rm -f /tmp/omni-shadow-{trace[:24]}.json").command
    return [
        {
            "purpose": "Gather current host and workload state before remediation.",
            "dry_run_command": inspect_cmd,
            "command": inspect_cmd,
            "target": "node:local",
            "risk_level": "low",
            "expected_output": "List of unhealthy pods/events for verification baseline.",
            "rollback_command": "echo 'readonly step - no rollback required'",
            "timeout_sec": 30,
            "evidence_refs": evidence_refs or [f"trace:{trace}"],
            "escalation_required": False,
        },
        {
            "purpose": f"Prepare controlled remediation payload derived from planner tool `{tn}`.",
            "dry_run_command": wrap_host_command(f"echo {json.dumps(raw_payload)}").command,
            "command": apply_cmd,
            "target": "node:local",
            "risk_level": "medium",
            "expected_output": "Payload file created for operator-reviewed execution.",
            "rollback_command": rollback_cmd,
            "timeout_sec": 45,
            "evidence_refs": evidence_refs or [f"trace:{trace}"],
            "escalation_required": True,
        },
    ]


def _symptom_group_from_batch(batch: list[dict[str, Any]]) -> str:
    """First non-empty symptom_group from diagnostic evidence batch (dispatcher sets per trace)."""
    for b in batch:
        sg = str(b.get("symptom_group") or "").strip()
        if sg:
            return sg
    return ""


def _is_siem_batch(batch: list[dict[str, Any]]) -> bool:
    """True when the evidence batch originates from a FinGuard/SIEM source."""
    return bool(_siem_alert_labels(batch))


_SIEM_CATEGORY_STEPS: dict[str, list[str]] = {
    "ddos": [
        "kubectl get networkpolicy -n {ns}",
        "kubectl get ingress -n {ns}",
        "kubectl get hpa -n {ns}",
        "Review WAF/ingress rate-limit config; apply IP block for affected_ip={ip}",
    ],
    "malware": [
        "kubectl get pods -n {ns} --show-labels",
        "kubectl get pods -n {ns} -o wide | grep {ip}",
        "kubectl get pods -n {ns} -o jsonpath='{{range .items[*]}}{{.metadata.name}}{{\"\\n\"}}{{end}}' | xargs -I{{}} kubectl describe pod {{}} -n {ns}",
        "Isolate: kubectl cordon <node-hosting-suspect-pod>; collect forensics before delete",
    ],
    "data_exfil": [
        "kubectl get networkpolicy -n {ns}",
        "kubectl get rolebindings,clusterrolebindings -n {ns}",
        "kubectl get pods -n {ns} -o wide | grep {ip}",
        "Audit egress traffic and RBAC; revoke over-permissive roles",
    ],
    "k8s_threat": [
        "kubectl get clusterrolebindings | grep -i admin",
        "kubectl get pods -n {ns} -o wide --show-labels",
        "kubectl get pods -n {ns} -o json | grep -E 'hostPID|privileged|hostNetwork'",
        "Check privilege escalation; review RBAC and pod securityContext",
    ],
    "auth_failure": [
        "kubectl logs -n {ns} -l app=auth --tail=200",
        "kubectl get serviceaccounts -n {ns}",
        "kubectl get rolebindings -n {ns}",
        "Rotate credentials; audit service account tokens",
    ],
    "lateral_movement": [
        "kubectl get networkpolicy -n {ns}",
        "kubectl get pods -n {ns} -o wide | grep {ip}",
        "kubectl get pods -n {ns} --show-labels",
        "Segment namespace: apply network policy deny-all; investigate pod-to-pod traffic",
    ],
}
_SIEM_DEFAULT_STEPS = [
    "kubectl get pods -n {ns} --show-labels",
    "kubectl get events -n {ns} --sort-by=.lastTimestamp",
    "kubectl get pods -n {ns} -o wide | grep {ip}",
    "Apply remediation from suggested_action below; verify with kubectl rollout status",
]


def _siem_diagnosis_from_batch(
    batch: list[dict[str, Any]],
    siem_labels: dict[str, str],
    sanitized_text: str,
) -> str:
    """Build an incident-specific diagnosis string from SIEM synthetic evidence."""
    ef: dict[str, Any] = {}
    for b in batch:
        if b.get("probe") == "siem_incident_context":
            raw_ef = b.get("extracted_fact")
            if isinstance(raw_ef, dict):
                ef = raw_ef
            elif isinstance(raw_ef, str):
                try:
                    ef = json.loads(raw_ef)
                except Exception:
                    pass
            break

    incident_id = ef.get("incident_id") or siem_labels.get("siem_incident_id", "n/a")
    category = ef.get("category") or siem_labels.get("siem_category", "unknown")
    severity = ef.get("severity") or siem_labels.get("severity", "")
    ns = ef.get("namespace") or siem_labels.get("namespace", "multi-agent")
    affected_ip = ef.get("affected_ip", "")
    description = ef.get("description", "")
    suggested = ef.get("suggested_action", "")
    tenant = ef.get("tenant") or siem_labels.get("siem_tenant", "")

    header = (
        f"[SIEM_INCIDENT] id={incident_id} category={category} severity={severity}"
        + (f" tenant={tenant}" if tenant else "")
        + (f"\naffected_ip: {affected_ip}" if affected_ip else "")
        + (f"\ndescription: {description}" if description else "")
        + (f"\nsuggested_action: {suggested}" if suggested else "")
    )

    raw_steps = _SIEM_CATEGORY_STEPS.get(category, _SIEM_DEFAULT_STEPS)
    steps = [s.format(ns=ns, ip=affected_ip or "?") for s in raw_steps]
    steps_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps))

    return (
        f"{header}\n\n"
        f"Operator next steps for [{category}] in namespace [{ns}]:\n"
        f"{steps_text}\n\n"
        "Omni does NOT auto-execute for SIEM incidents — human approval required."
    )


async def _notify_siem_telegram(
    ctx: WorkerHandlerContext,
    *,
    trace: str,
    batch: list[dict[str, Any]],
    diagnosis: str,
) -> None:
    """Send SIEM incident summary to the Telegram admin channel (fallback when no inbound chat)."""
    tg = getattr(ctx, "telegram", None)
    if tg is None:
        return
    admin_cid = getattr(ctx.settings, "telegram_admin_chat_id", None)
    if admin_cid is None:
        logger.warning("event=siem_suggest_no_admin_cid trace=%s — set OMNI_TELEGRAM_ADMIN_CHAT_ID", trace)
        return
    siem = _siem_alert_labels(batch)
    known: dict[str, str] = {
        "trace": trace,
        "incident_id": siem.get("siem_incident_id", "n/a"),
        "severity": siem.get("severity", "n/a"),
        "category": siem.get("siem_category", "n/a"),
    }
    ns = siem.get("namespace") or ""
    if ns:
        known["namespace"] = ns
    # Pull richer fields from siem_incident_context extracted_fact if present.
    # coerce_evidence_dict serializes extracted_fact → JSON string, so handle both.
    for b in batch:
        if b.get("probe") == "siem_incident_context":
            _ef_raw = b.get("extracted_fact")
            _ef: dict[str, Any] = {}
            if isinstance(_ef_raw, dict):
                _ef = _ef_raw
            elif isinstance(_ef_raw, str):
                try:
                    _parsed = json.loads(_ef_raw)
                    if isinstance(_parsed, dict):
                        _ef = _parsed
                except Exception:
                    pass
            if _ef.get("affected_ip"):
                known["affected_ip"] = _ef["affected_ip"]
            if _ef.get("description"):
                known["description"] = _ef["description"][:200]
            if _ef.get("tenant") and _ef["tenant"] != "unknown":
                known["tenant"] = _ef["tenant"]
            break
    # Extract actionable steps from the diagnosis (lines starting with digit.)
    step_lines = [
        ln.strip()
        for ln in diagnosis.splitlines()
        if ln.strip() and (ln.strip()[0].isdigit() or ln.strip().startswith("kubectl") or ln.strip().startswith("Review") or ln.strip().startswith("Apply") or ln.strip().startswith("Isolate") or ln.strip().startswith("Rotate") or ln.strip().startswith("Segment") or ln.strip().startswith("Audit") or ln.strip().startswith("Check"))
    ]
    if siem.get("suggested_action"):
        step_lines.append(f"SIEM recommendation: {siem['suggested_action'][:200]}")
    card_body = format_operator_action_card(
        known_facts=known,
        missing_facts=["automated execution blocked — SIEM incidents require human approval"],
        suggested_steps=step_lines or [f"Review: {diagnosis[:400]}"],
    )
    msg = (
        f"[SIEM] FinGuard incident — human execution required\n"
        f"{card_body}"
    )[:4096]
    try:
        await tg.send_message(int(admin_cid), msg)
        logger.info(
            "event=siem_suggest_telegram_sent trace=%s siem_incident=%s",
            trace, siem.get("siem_incident_id", ""),
        )
    except Exception as e:
        logger.warning("event=siem_suggest_telegram_error trace=%s err=%s", trace, e)


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


def _oom_memory_planner_note_from_batch(batch: list[dict[str, Any]]) -> str | None:
    """When pod is OOMKilled and we have a memory figure (spec fallback or PodMetrics), nudge planner past empty Prom."""
    has_oom = False
    mem_line = ""
    for b in batch:
        ef = b.get("extracted_fact")
        if not isinstance(ef, dict):
            continue
        probe = str(b.get("probe") or "")
        if probe == "k8s_clinical_pod_status" and ef.get("has_oom_killed") is True:
            has_oom = True
        if probe != "k8s_clinical_pod_metrics":
            continue
        kind = str(ef.get("kind") or "")
        ctrs = ef.get("containers")
        if not isinstance(ctrs, list):
            continue
        for c in ctrs:
            if not isinstance(c, dict):
                continue
            m = c.get("memory")
            if m:
                mem_line = str(m).strip()
                break
        if mem_line and kind in ("PodMetricsSpecFallback", "PodMetrics"):
            break
    if has_oom and mem_line:
        return (
            "OOMKilled; last known container memory from SDK: "
            f"{mem_line}. prom_pod_memory_wss may be empty after termination — "
            "raise Deployment memory via k8s_patch_resource using this baseline."
        )
    return None


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
        oom_note = _oom_memory_planner_note_from_batch(batch)
        if oom_note:
            h.setdefault("oom_memory_planner_note", oom_note[:1200])
    return h if h else None


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _planner_phase_done_diagnosis(fa: str, rs: str) -> str:
    """Prefer resolution_summary for PLANNER_PHASE_DONE; merge with final_analysis when both differ."""
    fa_s = (fa or "").strip()
    rs_s = (rs or "").strip()
    if rs_s and fa_s and rs_s != fa_s:
        return f"{rs_s}\n\n{fa_s}"
    return rs_s or fa_s or "Planner concluded diagnostic session; see reasoning_chain."


async def _planner_missing_preconditions(
    ctx: WorkerHandlerContext,
    *,
    trace: str,
    tool_name: str,
    args: dict[str, Any],
    discovery_steps: list[str],
    planner_missing: list[str] | None = None,
) -> list[str]:
    """Validate mutate preconditions from tool metadata + planner-declared gaps."""
    if not bool(getattr(ctx.settings, "omni_planner_precondition_gate_enabled", True)):
        return []
    reg = get_tool_registry()
    if not reg.has(tool_name):
        return [f"unknown_tool:{tool_name}"]
    meta = reg.metadata_for(tool_name)
    schema = reg.json_schema_for(tool_name)
    schema_props = schema.get("properties") if isinstance(schema, dict) else {}
    schema_props = schema_props if isinstance(schema_props, dict) else {}
    missing: list[str] = []
    mem = await load_trace_memory(
        ctx.redis,
        trace,
        initial_symptoms="",
        initial_symptom=None,
    )
    readonly_ok_actions = [
        a
        for a in (mem.action_history or [])
        if str(a.kind) == "readonly_executed" and not bool(a.is_error)
    ]
    has_discovery = bool(discovery_steps) or bool(readonly_ok_actions)

    def _has_field_value(field_name: str) -> bool:
        if field_name == "value":
            return "value" in args
        raw = args.get(field_name)
        if isinstance(raw, str):
            if raw.strip():
                return True
        elif raw is not None:
            return True
        prop = schema_props.get(field_name) if isinstance(schema_props, dict) else None
        if isinstance(prop, dict) and "default" in prop:
            default_v = prop.get("default")
            if isinstance(default_v, str):
                return bool(default_v.strip())
            return default_v is not None
        return False

    for fld in [str(x) for x in (meta.get("required_fields") or []) if str(x).strip()]:
        if not _has_field_value(fld):
            missing.append(f"arg:{fld}")
    if bool(meta.get("requires_readonly_before_mutate", False)) and not has_discovery:
        missing.append("readonly_discovery_evidence")

    def _evidence_satisfied(tag: str) -> bool:
        t = str(tag or "").strip().lower()
        if not t:
            return True
        if t == "secret_ref_confirmed":
            if "k8s_get_pod_secret_refs" in discovery_steps:
                return True
            return any(str(a.tool_name) == "k8s_get_pod_secret_refs" for a in readonly_ok_actions)
        if t == "credential_source_of_truth":
            src = str(args.get("value_source") or "").strip()
            src_ref = str(args.get("value_source_ref") or "").strip()
            return bool(src and src_ref)
        if t == "target_workload_identity":
            return bool(
                str(args.get("deployment") or "").strip()
                or str(args.get("pod_name") or "").strip()
                or str(args.get("name") or "").strip()
            )
        if t == "patch_target_confirmed":
            return has_discovery
        if t == "rbac_drift_signal":
            return has_discovery
        return has_discovery

    for ev in [str(x).strip() for x in (meta.get("required_evidence") or []) if str(x).strip()]:
        if not _evidence_satisfied(ev):
            missing.append(f"evidence:{ev}")

    for item in [str(x).strip() for x in (planner_missing or []) if str(x).strip()]:
        if item.startswith("arg:"):
            fld = item.split(":", 1)[1].strip()
            if fld and _has_field_value(fld):
                continue
        if item == "readonly_discovery_evidence" and has_discovery:
            continue
        if item.startswith("evidence:"):
            ev = item.split(":", 1)[1].strip()
            if ev and _evidence_satisfied(ev):
                continue
        if item not in missing:
            missing.append(item)
    return missing


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
    if _shadow_os_mode(ctx) and suggested_tool in MUTATE_TOOL_ALLOWLIST:
        commands = _derive_shadow_os_commands(
            tool_name=suggested_tool,
            args={},
            evidence_refs=[f"trace:{tid}", "source:suggest_remediation"],
            trace=tid,
        )
        emitted = await _emit_suggest_os_runbook(
            ctx,
            trace=tid,
            diagnosis=diagnosis,
            confidence=_clamp01(confidence),
            source=f"{source}_SHADOW_OS",
            runbook_title=f"Shadow runbook for {suggested_tool}",
            commands=commands,
            reasoning_chain=reasoning_chain if isinstance(reasoning_chain, dict) else {},
            verification_evidence_digest=str(diagnosis)[:800],
        )
        if emitted:
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


async def _emit_suggest_os_runbook(
    ctx: WorkerHandlerContext,
    *,
    trace: str,
    diagnosis: str,
    confidence: float,
    source: str,
    runbook_title: str,
    commands: list[dict[str, Any]],
    reasoning_chain: dict[str, Any] | None = None,
    verification_evidence_digest: str = "",
) -> bool:
    """Emit Shadow OS runbook action to omni-actions."""
    if not ctx.settings.trace_correlation_ping_enabled:
        return False
    k = ctx.kafka
    if k is None:
        return False
    tid = str(trace or "").strip()
    if not tid:
        return False
    body = build_suggest_os_runbook_body(
        tid,
        diagnosis=diagnosis,
        confidence=confidence,
        source=source,
        runbook_title=runbook_title,
        commands=commands,
        reasoning_chain=reasoning_chain,
        verification_evidence_digest=verification_evidence_digest,
    )
    try:
        data_obj = body.get("data") if isinstance(body.get("data"), dict) else {}
        validate_suggest_os_runbook_data(data_obj)
    except Exception as e:
        logger.warning("event=shadow_runbook_invalid trace=%s err=%s", tid, e)
        return False
    try:
        await k.send_dict(ctx.settings.kafka_topic_actions, {"data": json.dumps(body, ensure_ascii=False)})
        logger.info(
            "event=action_emitted action=SUGGEST_OS_RUNBOOK trace=%s source=%s steps=%s",
            tid,
            source,
            len(commands),
        )
        return True
    except Exception as e:
        logger.warning("shadow_runbook_emit skip: %s", e)
        return False


async def _emit_agentic_mutate_if_any(
    ctx: WorkerHandlerContext,
    trace: str,
    batch: list[dict[str, Any]],
    *,
    sanitized_text: str,
    rag_match_text: str | None = None,
    rag_reasoning_hints: str | None = None,
    attempt_count: int = 1,
    playbook: "Any | None" = None,
) -> bool:
    """
    Planner-first mutate emission:
    - optional blind proof_lane hint (matrix miss) before planner
    - always ask LLM planner (max N steps) using Fact Table + sanitized context
    - proof_of_fault gate (with blind lane hint)
    - diagnostic invariant gate (INV_*) before EXECUTE_MUTATE

    Returns True if EXECUTE_MUTATE was emitted; False otherwise (suggest-only, blocked, or no plan).
    """
    ac = max(1, int(attempt_count))

    # SIEM suggest-only: FinGuard incidents must not enter EXECUTE_MUTATE or HITL pipeline.
    # Emit SUGGEST_REMEDIATION + Telegram to admin; return False without touching the planner.
    if bool(getattr(ctx.settings, "omni_siem_suggest_only", True)) and _is_siem_batch(batch):
        siem = _siem_alert_labels(batch)
        incident_id = siem.get("siem_incident_id", "")
        diag = _siem_diagnosis_from_batch(batch, siem, sanitized_text)
        await _emit_suggest_remediation(
            ctx,
            trace=trace,
            diagnosis=diag,
            confidence=0.9,
            source="SIEM_SUGGEST_ONLY",
            suggested_tool="k8s_describe_resource",
        )
        await _notify_siem_telegram(ctx, trace=trace, batch=batch, diagnosis=diag)
        await emit_transition(
            ctx,
            trace_id=trace,
            transition=TRANSITION_PLAN_EMITTED,
            component="evidence_consumer",
            detail="siem_suggest_only",
            meta={"siem_incident_id": incident_id},
        )
        logger.info(
            "event=siem_suggest_only_emitted trace=%s siem_incident=%s",
            trace, incident_id,
        )
        return True  # handled — caller must not fall through to RAG_MISS escalation

    llm_first = bool(getattr(ctx.settings, "omni_llm_first_autonomy_enabled", False))
    unrestricted = bool(getattr(ctx.settings, "omni_unrestricted_tool_execution", True))
    legacy_det_fallback = bool(getattr(ctx.settings, "omni_legacy_deterministic_fallback", False))
    # In LLM-first mode, force planner-led diagnose→verify→mutate flow.
    # "unrestricted" controls execution gates, not whether deterministic shortcuts bypass planner.
    allow_det = legacy_det_fallback and not llm_first
    blind_pre = await infer_blind_proof_lane_hint(
        ctx, batch, sanitized_text=sanitized_text, rag_match_text=rag_match_text
    )
    initial_symptom = initial_symptom_from_evidence_batch(batch)
    det_plan = None
    if allow_det:
        det_plan = deterministic_mutate_plan_from_batch(
            batch,
            default_ns=default_remediation_namespace(ctx.settings),
            allowed_tools=probe_driven_mutate_tools_for_settings(ctx.settings),
            ws=ctx.settings,
        )
    if det_plan:
        logger.info(
            "event=probe_deterministic_mutate trace=%s tool=%s",
            trace,
            det_plan.get("tool_name"),
        )
        plan = det_plan
        plan["planner_origin"] = "deterministic"
        discovery_steps: list[str] = list(det_plan.get("discovery_steps") or [])
    else:
        lane_for_mx, _ls = resolve_proof_lane(batch, rag_match_text=rag_match_text)
        mx = int(getattr(ctx.settings, "autonomous_agentic_max_steps", 5) or 5)
        if lane_for_mx == "state":
            mx = max(mx, 8)
        # Recall similar verified playbooks from pgvector (advisory only; no secret values injected).
        recall_result = await recall_playbook_advisory(
            ctx, query_text=sanitized_text, trace=trace
        )
        strong_prefix: str | None = None
        if recall_result:
            # Suppress strong recall prefix when evidence clearly shows credential failure.
            # The recalled playbook may be from a different incident type (e.g. ConfigMap fix vs.
            # credential patch) — injecting it as a priority prefix would mislead the LLM.
            # The credential_hint in analyst_agentic_loop will handle this case instead.
            recall_is_wrong_type = (
                recall_result.strong
                and _evidence_suggests_credential_failure(batch)
                and recall_result.top_tool != "k8s_patch_secret"
            )
            if recall_result.strong and not recall_is_wrong_type:
                # High-confidence hit (>= 0.85): inject as priority prefix before Fact Table.
                strong_prefix = build_strong_recall_prefix(recall_result)
                logger.info(
                    "event=archivist_strong_recall_injected trace=%s top_tool=%s score=%.3f",
                    trace, recall_result.top_tool, recall_result.top_score,
                )
            else:
                # Low-confidence or suppressed: soft advisory appended to rag_reasoning_hints.
                if recall_is_wrong_type:
                    logger.info(
                        "event=archivist_strong_recall_suppressed trace=%s top_tool=%s score=%.3f reason=credential_failure_mismatch",
                        trace, recall_result.top_tool, recall_result.top_score,
                    )
                soft = recall_result.advisory
                if not rag_reasoning_hints:
                    rag_reasoning_hints = soft
                else:
                    rag_reasoning_hints = f"{rag_reasoning_hints}\n\n{soft}"
        plan = await run_agentic_mutate_plan(
            ctx,
            trace=trace,
            sanitized_text=sanitized_text,
            batch=batch,
            max_steps=mx,
            rag_reasoning_hints=rag_reasoning_hints,
            recall_prefix=strong_prefix,
            initial_symptom=initial_symptom,
            playbook=playbook,
        )
        discovery_steps = list(plan.get("discovery_steps") or []) if plan else []
        # LLM-first skips deterministic_mutate_plan_from_batch (including chaos lab autofix).
        # When the planner exhausts without a mutate tool (None, escalate, loop abort), retry the
        # lab-only k8s_patch_secret plan so credential chaos can still self-heal without escalation.
        rc_pre = str((plan or {}).get("reason_code") or "")
        tn_pre = str((plan or {}).get("tool_name") or "").strip()
        if (
            rc_pre != PLANNER_PHASE_DONE
            and (plan is None or not tn_pre)
            and bool(getattr(ctx.settings, "lab_chaos_credential_autofix_enabled", False))
        ):
            _tools_cf = probe_driven_mutate_tools_for_settings(ctx.settings)
            _tools_cf = frozenset(_tools_cf) | frozenset({"k8s_patch_secret"})
            chaos_p = chaos_credential_lab_autofix_plan_from_batch(
                batch,
                default_ns=default_remediation_namespace(ctx.settings),
                allowed_tools=_tools_cf,
                ws=ctx.settings,
            )
            if chaos_p:
                plan = chaos_p
                plan["planner_origin"] = "chaos_lab_autofix_after_planner"
                discovery_steps = list(chaos_p.get("discovery_steps") or [])
                logger.info(
                    "event=chaos_lab_autofix_after_planner trace=%s tool=%s",
                    trace,
                    chaos_p.get("tool_name"),
                )
    if plan and str(plan.get("reason_code") or "") == PLANNER_PHASE_DONE:
        fa = str(plan.get("final_analysis") or "").strip()
        rs = str(plan.get("resolution_summary") or "").strip()
        rc = plan.get("reasoning_chain") if isinstance(plan.get("reasoning_chain"), dict) else None
        tp_done: list[str] = []
        if rc and isinstance(rc.get("thought_process"), list):
            tp_done = [str(x) for x in rc["thought_process"]][:32]
        lane_g = str(rc.get("lane") or "state") if isinstance(rc, dict) else "state"
        diag_done = _planner_phase_done_diagnosis(fa, rs)
        await _emit_suggest_remediation(
            ctx,
            trace=trace,
            diagnosis=diag_done,
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
            meta={
                "phase": "done",
                "reason_code": PLANNER_PHASE_DONE,
                "resolution_summary": rs,
            },
        )
        logger.info("event=planner_phase_done_emitted trace=%s", trace)
        return False
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
        if not allow_det:
            logger.info(
                "event=planner_readonly_route_no_deterministic_fallback trace=%s llm_first=%s",
                trace,
                llm_first,
            )
            return False
        # Legacy continue path: synthesize rollout restart from evidence so proof-of-fault + INV_* gates still run.
        lane_saved = plan.get("lane_hint") if isinstance(plan.get("lane_hint"), str) else None
        rr = rollout_args_from_evidence_batch(batch)
        is_fault = workload_fault_incident_rollout_eligible(batch)
        is_cpu = workload_cpu_incident_rollout_eligible(batch)
        if not rr or not (is_fault or is_cpu):
            return False
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
    fallback_lane_override: str | None = None
    if not plan:
        if not allow_det:
            await _emit_suggest_remediation(
                ctx,
                trace=trace,
                diagnosis=(
                    "Planner did not emit a mutate plan and deterministic fallback is disabled "
                    "(LLM-first mode). Continue discovery or escalate."
                ),
                confidence=0.4,
                source="PLANNER_UNAVAILABLE_NO_LEGACY_FALLBACK",
                suggested_tool="k8s_describe_resource",
            )
            await emit_transition(
                ctx,
                trace_id=trace,
                transition=TRANSITION_PLAN_EMITTED,
                component="evidence_consumer",
                detail="planner_unavailable_no_legacy_fallback",
            )
            return False
        # Planner-first failed (LLM unavailable/invalid JSON): safe deterministic fallback
        # only for clearly identified workload incidents.
        rr = rollout_args_from_evidence_batch(batch)
        if not rr:
            return False
        is_fault = workload_fault_incident_rollout_eligible(batch)
        is_cpu = workload_cpu_incident_rollout_eligible(batch)
        if not (is_fault or is_cpu):
            return False

        # INV_NO_RESTART_ON_BROKEN_SPEC: route based on missing resource type.
        # ConfigMap absent  → k8s_create_or_patch_configmap (idempotent placeholder creation).
        # Secret absent     → k8s_rollout_restart kept; INV_NO_RESTART_ON_BROKEN_SPEC will block
        #                     it and emit SUGGEST_REMEDIATION with invariant context (Secrets need
        #                     real values; automation cannot supply them safely).
        # Both cases set fallback_lane_override="state" so the proof gate fast-tracks without
        # requiring metric sigma (broken-spec is a deterministic K8s failure, not a metric spike).
        _cm_re = re.compile(r'configmap\s+"([^"]+)"\s+not\s+found', re.IGNORECASE)
        _sec_re = re.compile(r'secret\s+"([^"]+)"\s+not\s+found', re.IGNORECASE)
        _batch_blob = " ".join(
            str(b.get("raw") or "") + " " + json.dumps(b.get("extracted_fact") or "")
            for b in batch
        )
        _cm_match = _cm_re.search(_batch_blob)
        _sec_match = _sec_re.search(_batch_blob)
        if evidence_suggests_broken_spec(batch) and _cm_match:
            cm_name = _cm_match.group(1)
            tn = "k8s_create_or_patch_configmap"
            args = {
                "namespace": rr["namespace"],
                "name": cm_name,
                "key": "placeholder",
                "value": "created-by-omni",
                "reasoning": (
                    f"ConfigMap '{cm_name}' absent (FailedMount/CreateContainerConfigError); "
                    "creating with placeholder key to unblock pod volume mount."
                ),
            }
            fallback_lane_override = "state"
            logger.warning(
                "event=agentic_mutate_fallback trace=%s tool=%s reason=broken_spec_cm_absent cm=%s",
                trace,
                tn,
                cm_name,
            )
        elif evidence_suggests_broken_spec(batch) and _sec_match:
            # Secret absent: do NOT emit rollout_restart (meaningless for source-of-truth fault).
            # Fail closed to suggest/escalate with explicit source-fix requirement.
            sec_name = _sec_match.group(1)
            await _emit_suggest_remediation(
                ctx,
                trace=trace,
                diagnosis=(
                    f"Secret '{sec_name}' is absent (broken spec). Rollout restart is not a fix. "
                    "Provide/restore secret data, then re-run verify."
                ),
                confidence=0.65,
                source="BROKEN_SPEC_SECRET_ABSENT",
                suggested_tool="k8s_describe_resource",
            )
            await emit_transition(
                ctx,
                trace_id=trace,
                transition=TRANSITION_PLAN_EMITTED,
                component="evidence_consumer",
                detail="broken_spec_secret_absent_no_rollout",
                meta={"secret": sec_name, "reason_code": "BROKEN_SPEC_SECRET_ABSENT"},
            )
            logger.warning(
                "event=agentic_mutate_fallback trace=%s reason=broken_spec_secret_absent_no_rollout secret=%s",
                trace,
                sec_name,
            )
            return False
        else:
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
            return False
    # Rollout cannot fix DB/API password drift; LLM or legacy fault fallback may still pick it.
    if (
        tn == "k8s_rollout_restart"
        and _evidence_suggests_credential_failure(batch)
        and bool(getattr(ctx.settings, "lab_chaos_credential_autofix_enabled", False))
    ):
        _tools_veto = frozenset(probe_driven_mutate_tools_for_settings(ctx.settings)) | frozenset(
            {"k8s_patch_secret"}
        )
        chaos_swap = chaos_credential_lab_autofix_plan_from_batch(
            batch,
            default_ns=default_remediation_namespace(ctx.settings),
            allowed_tools=_tools_veto,
            ws=ctx.settings,
        )
        if chaos_swap:
            plan = chaos_swap
            plan["planner_origin"] = "chaos_lab_autofix_veto_rollout_restart"
            discovery_steps = list(chaos_swap.get("discovery_steps") or [])
            tn = str(chaos_swap.get("tool_name") or "").strip()
            args = dict(chaos_swap.get("args") or {})
            logger.info(
                "event=chaos_lab_autofix_veto_rollout_restart trace=%s tool=%s",
                trace,
                tn,
            )
    # Lab: planner may emit k8s_patch_secret with placeholder "value" — substitute settings-backed password.
    # Mirrors chaos_lab_autofix_veto_rollout_restart: same batch + OMNI_LAB_CHAOS_CREDENTIAL_AUTOFIX_ENABLED.
    if (
        tn == "k8s_patch_secret"
        and _evidence_suggests_credential_failure(batch)
        and bool(getattr(ctx.settings, "lab_chaos_credential_autofix_enabled", False))
    ):
        _tools_lab = frozenset(probe_driven_mutate_tools_for_settings(ctx.settings)) | frozenset(
            {"k8s_patch_secret"}
        )
        chaos_patch = chaos_credential_lab_autofix_plan_from_batch(
            batch,
            default_ns=default_remediation_namespace(ctx.settings),
            allowed_tools=_tools_lab,
            ws=ctx.settings,
        )
        if chaos_patch:
            plan = {**(plan if isinstance(plan, dict) else {}), **chaos_patch}
            plan["planner_origin"] = "chaos_lab_autofix_override_llm_patch_secret"
            discovery_steps = list(chaos_patch.get("discovery_steps") or [])
            tn = str(chaos_patch.get("tool_name") or "").strip()
            args = dict(chaos_patch.get("args") or {})
            logger.info(
                "event=chaos_lab_autofix_override_llm_patch_secret trace=%s tool=%s",
                trace,
                tn,
            )
    plan_origin = str((plan or {}).get("planner_origin") or "llm")
    if (
        plan
        and bool(getattr(ctx.settings, "omni_planner_precondition_gate_enabled", True))
        and (not unrestricted)
        and plan_origin != "deterministic"
    ):
        planner_missing = (
            [str(x).strip() for x in (plan.get("missing_preconditions") or []) if str(x).strip()]
            if isinstance(plan.get("missing_preconditions"), list)
            else []
        )
        missing = await _planner_missing_preconditions(
            ctx,
            trace=trace,
            tool_name=tn,
            args=args,
            discovery_steps=discovery_steps,
            planner_missing=planner_missing,
        )
        if missing:
            retry_steps = max(1, min(4, int(getattr(ctx.settings, "autonomous_agentic_max_steps", 5) or 5)))
            retry_hints = (
                f"{rag_reasoning_hints or ''}\n\n"
                "[precondition_gate_reask]\n"
                f"Rejected mutate tool={tn} due to missing preconditions: {', '.join(missing)}.\n"
                "Choose discovery action to collect missing evidence, or escalate if blocked."
            ).strip()
            plan_retry = await run_agentic_mutate_plan(
                ctx,
                trace=trace,
                sanitized_text=sanitized_text,
                batch=batch,
                max_steps=retry_steps,
                rag_reasoning_hints=retry_hints,
                recall_prefix=None,
                initial_symptom=initial_symptom,
            )
            if plan_retry and str(plan_retry.get("reason_code") or "") == PLANNER_PHASE_DONE:
                diag_done = _planner_phase_done_diagnosis(
                    str(plan_retry.get("final_analysis") or ""),
                    str(plan_retry.get("resolution_summary") or ""),
                )
                await _emit_suggest_remediation(
                    ctx,
                    trace=trace,
                    diagnosis=diag_done,
                    confidence=0.6,
                    source="PLANNER_PRECONDITION_REASK_DONE",
                    suggested_tool="k8s_describe_resource",
                )
                await emit_transition(
                    ctx,
                    trace_id=trace,
                    transition=TRANSITION_PLAN_EMITTED,
                    component="evidence_consumer",
                    detail="planner_precondition_reask_done",
                )
                return False
            if plan_retry:
                plan = plan_retry
                discovery_steps = list(plan_retry.get("discovery_steps") or [])
                tn = str(plan_retry.get("tool_name") or "").strip()
                args = dict(plan_retry.get("args") or {})
                missing = await _planner_missing_preconditions(
                    ctx,
                    trace=trace,
                    tool_name=tn,
                    args=args,
                    discovery_steps=discovery_steps,
                    planner_missing=(
                        [str(x).strip() for x in (plan_retry.get("missing_preconditions") or []) if str(x).strip()]
                        if isinstance(plan_retry.get("missing_preconditions"), list)
                        else []
                    ),
                )
            if missing:
                await _emit_suggest_remediation(
                    ctx,
                    trace=trace,
                    diagnosis=(
                        "Mutate blocked by planner precondition gate. Missing: "
                        + ", ".join(missing[:8])
                    ),
                    confidence=0.45,
                    source="PLANNER_PRECONDITION_GATE",
                    suggested_tool="k8s_describe_resource",
                )
                await emit_transition(
                    ctx,
                    trace_id=trace,
                    transition=TRANSITION_PLAN_EMITTED,
                    component="evidence_consumer",
                    detail="planner_precondition_gate_blocked",
                    meta={"missing_preconditions": missing[:12], "tool": tn},
                )
                return False
    if (not unrestricted) and tn not in MUTATE_TOOL_ALLOWLIST:
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
        return False
    blind_lane_eff: str | None = blind_pre
    if plan:
        lh_raw = plan.get("lane_hint")
        if isinstance(lh_raw, str) and lh_raw.strip():
            blind_lane_eff = lh_raw.strip()
    elif fallback_lane_override:
        blind_lane_eff = fallback_lane_override
    if unrestricted:
        proof_ok, reason_code, proof_meta = True, "", {"proof_lane": blind_lane_eff or "unknown"}
    else:
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
                return False
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
            return False
    pl_inv = proof_meta.get("proof_lane")
    proof_lane_for_inv = str(pl_inv).strip() if isinstance(pl_inv, str) and pl_inv.strip() else None
    if unrestricted:
        inv_ok, inv_reason, inv_meta = True, "", {}
    else:
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
        return False
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
    if _shadow_os_mode(ctx):
        evidence_refs = [f"proof_lane:{proof_meta.get('proof_lane')}", f"trace:{trace}"]
        commands = _derive_shadow_os_commands(
            tool_name=tn,
            args=args,
            evidence_refs=evidence_refs,
            trace=trace,
        )
        emitted = await _emit_suggest_os_runbook(
            ctx,
            trace=trace,
            diagnosis=f"Planner produced mutate intent `{tn}`; converted to Shadow OS runbook.",
            confidence=0.72,
            source="SHADOW_OS_FROM_MUTATE_PLAN",
            runbook_title=f"Shadow remediation for {tn}",
            commands=commands,
            reasoning_chain=exec_rc if isinstance(exec_rc, dict) else {},
            verification_evidence_digest=json.dumps(proof_meta, ensure_ascii=False)[:1500],
        )
        if emitted:
            await emit_transition(
                ctx,
                trace_id=trace,
                transition=TRANSITION_PLAN_EMITTED,
                component="evidence_consumer",
                detail=f"shadow_os_runbook_emitted:{tn}",
                meta={"shadow_os_mode": True, "step_count": len(commands)},
            )
            return True
        return False
    # HITL gate: SIEM-sourced critical incidents must pause for human approval.
    if _siem_hitl_required(batch):
        await emit_hitl_pending(
            ctx,
            trace=trace,
            tool_name=tn,
            args=args,
            attempt_count=ac,
            reasoning_chain=exec_rc if isinstance(exec_rc, dict) else None,
            hitl_reason="siem_critical_action_requires_approval",
            batch=batch,
            explain=str((plan or {}).get("explain") or ""),
            advise=str((plan or {}).get("advise") or ""),
        )
    else:
        await emit_execute_mutate(
            ctx,
            trace=trace,
            tool_name=tn,
            args=args,
            attempt_count=ac,
            reasoning_chain=exec_rc if isinstance(exec_rc, dict) else None,
        )
    return True


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
        batch = await merge_preflight_deployment_secret_refs(batch, trace=trace)

        # Match a pre-approved playbook for this incident (advisory; None = fall through to generic flow).
        matched_playbook = None
        try:
            _r = getattr(getattr(ctx, "vector_store", None), "_r", None)
            if _r is not None:
                _matcher = PlaybookMatcher(PlaybookStore(_r))
                matched_playbook = await _matcher.match_from_batch(batch)
                if matched_playbook:
                    logger.info(
                        "event=playbook_matched trace=%s playbook_id=%s",
                        trace,
                        matched_playbook.playbook_id,
                    )
        except Exception as _pm_err:
            logger.warning("event=playbook_match_error trace=%s err=%s", trace, _pm_err)

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
            an = alertname_from_batch(batch)
            score = gate_out.best_score
            rag_low_for_security = (
                an in _SECURITY_SELF_REMEDIATION_ALERT_NAMES
                and score is not None
                and float(score) < _RAG_SCORE_FLOOR_SECURITY
            )
            if rag_low_for_security:
                logger.info(
                    "event=rag_security_score_floor trace=%s alertname=%s score=%s floor=%s",
                    trace,
                    an,
                    score,
                    _RAG_SCORE_FLOOR_SECURITY,
                )
            security_hardening = _symptom_group_from_batch(batch) == "security_hardening"
            if security_hardening:
                logger.info(
                    "event=rag_security_hardening_bypass trace=%s symptom_group=security_hardening",
                    trace,
                )
            intercept_rag_suggest = (
                (proof_lane_pre == "state")
                or broken
                or rag_low_for_security
                or security_hardening
            )

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
                    playbook=matched_playbook,
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
                ctx, trace, batch, sanitized_text=sanitized_text, rag_match_text=rag_txt,
                playbook=matched_playbook,
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
            # Inject available identity so the model can propose scoped read-only tools
            # instead of ESCALATE-with-empty-action when namespace/deployment are known.
            _batch_identity = _identity_from_batch(batch)
            _id_prefix = _build_identity_prefix(_batch_identity)
            _sdk_text = (_id_prefix + analyst_text) if _id_prefix else analyst_text
            sdk_payload: dict[str, Any] = {
                "trace_id": trace,
                "source": "diagnostic_evidence",
                "text": _sdk_text,
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
                # Post-parse guardrail: if namespace is known but action.tool is empty, emit a
                # scoped read-only suggestion so operators get actionable steps rather than a blank escalate.
                _action_obj = machine.get("action") if isinstance(machine.get("action"), dict) else {}
                _action_tool = str((_action_obj or {}).get("tool") or "").strip()
                if not _action_tool and _batch_identity.get("namespace"):
                    _ns = _batch_identity["namespace"]
                    _dep = _batch_identity.get("deployment", "")
                    _scoped_tool = "k8s_describe_resource"
                    if _dep:
                        _scoped_diag = (
                            f"PARTIAL_IDENTITY_ESCALATE: namespace={_ns} deployment={_dep}\n"
                            f"Read-only next step: describe deployment then inspect pods.\n"
                            f"kubectl describe deployment {_dep} -n {_ns}\n"
                            f"kubectl get pods -n {_ns} -l app={_dep} --show-labels"
                        )
                    else:
                        _scoped_diag = (
                            f"PARTIAL_IDENTITY_ESCALATE: namespace={_ns}\n"
                            f"Read-only next step: list pods in known namespace.\n"
                            f"kubectl get pods -n {_ns} --show-labels"
                        )
                    await _emit_suggest_remediation(
                        ctx,
                        trace=trace,
                        diagnosis=_scoped_diag,
                        confidence=0.25,
                        source="SDK_PARTIAL_IDENTITY_SUGGEST",
                        suggested_tool=_scoped_tool,
                    )
                    logger.info(
                        "event=sdk_partial_identity_suggest trace=%s ns=%s dep=%s",
                        trace, _ns, _dep or "n/a",
                    )
                # Run full ReAct + CoT + mutate pipeline before any human escalation. SDK-only LLM
                # may return ESCALATE when it cannot name a tool; the agentic planner may still proceed.
                planner_emitted = await _emit_agentic_mutate_if_any(
                    ctx, trace, batch, sanitized_text=sanitized_text, playbook=matched_playbook,
                )
                if planner_emitted:
                    return display_out

                _tg_known: dict[str, str] = {}
                if batch:
                    _ar = str(batch[0].get("alert_rule") or "").strip()[:80]
                    _ah = str(batch[0].get("alert_hint") or "").strip()[:120]
                    if _ar:
                        _tg_known["alert"] = _ar
                    if _ah:
                        _tg_known["hint"] = _ah
                _tg_known.update(_batch_identity)
                _tg_missing: list[str] = []
                if not _batch_identity.get("namespace"):
                    _tg_missing.append("namespace unknown")
                if not _batch_identity.get("pod"):
                    _tg_missing.append("pod name")
                if not _batch_identity.get("deployment"):
                    _tg_missing.append("deployment name")
                _tg_missing.append("RAG knowledge: miss — no matching runbook")
                if human:
                    _tg_missing.append(f"LLM: {human[:200]}")
                _tg_steps: list[str] = []
                _tg_ns = _batch_identity.get("namespace")
                _tg_dep = _batch_identity.get("deployment")
                if _tg_ns and _tg_dep:
                    _tg_steps.append(f"kubectl describe deployment {_tg_dep} -n {_tg_ns}")
                    _tg_steps.append(f"kubectl get pods -n {_tg_ns} -l app={_tg_dep} --show-labels")
                elif _tg_ns:
                    _tg_steps.append(f"kubectl get pods -n {_tg_ns} --show-labels")
                _tg_steps.append("review runbook or escalate to on-call")
                _tg_card = format_operator_action_card(
                    known_facts=_tg_known,
                    missing_facts=_tg_missing,
                    suggested_steps=_tg_steps,
                )
                _tg_alertname = alertname_from_batch(batch)
                _SYNTHETIC_ALERTNAMES = {"FullAudit", "ChaosLabAlert"}
                if _tg_alertname in _SYNTHETIC_ALERTNAMES:
                    logger.info(
                        "event=telegram_escalation_suppressed trace=%s reason=synthetic_audit alertname=%s",
                        trace,
                        _tg_alertname,
                    )
                else:
                    await emit_telegram_escalation(
                        ctx,
                        trace,
                        _tg_card,
                        reason="RAG_MISS_SDK_ESCALATE",
                    )
                # Auto-execute lab: suggest path only when planner also failed to emit mutate.
                if not bool(getattr(ctx.settings, "omni_auto_execute_enabled", False)):
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
            await store_autonomous_trace_context(ctx.redis, trace, batch=batch, sanitized_text=sanitized_text)
            await emit_transition(
                ctx,
                trace_id=trace,
                transition=TRANSITION_PLAN_EMITTED,
                component="evidence_consumer",
                detail=f"sdk_only_plan:{tool or 'none'}",
            )
            planner_emitted = await _emit_agentic_mutate_if_any(
                ctx, trace, batch, sanitized_text=sanitized_text, playbook=matched_playbook,
            )
            if not planner_emitted:
                await _emit_suggest_remediation(
                    ctx,
                    trace=trace,
                    diagnosis=f"{human}\n[{hyp}]"[:4000],
                    confidence=0.55,
                    source="SDK_FACTS_ONLY" if rag_gate_failed else "SDK_ONLY_DIAGNOSE",
                    suggested_tool=tool or "inspect_pod_logs",
                )

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
            await _emit_agentic_mutate_if_any(
                ctx, trace, batch, sanitized_text=sanitized_text, playbook=matched_playbook,
            )
        if chat_id is not None:
            await send_telegram_out_for_inbound(ctx, payload, trace, out)
        return out
    finally:
        pop_trace_id(tok)
