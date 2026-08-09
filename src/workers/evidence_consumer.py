"""Consume ``omni-diagnostic-evidence`` — read-only reasoning; emit SUGGEST_REMEDIATION to omni-actions.

Planner (`run_agentic_mutate_plan`) may use InitialSymptom + sole-evaluator prompts; deterministic mutate,
proof-of-fault, and diagnostic policy gates below still apply and are not overridden by the LLM planner alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from pkg.domain.taxonomy import UNKNOWN as _DOMAIN_UNKNOWN, normalize_domain
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
from workers.alert_sdk_truth_compare import (
    build_contrast_diagnosis_for_action,
    build_contrast_operator_telegram_body,
    compare_alert_claim_to_sdk_state,
)
from workers.os_state_validator import compare_alert_claim_to_os_state
from workers.siem_reasoning import (
    extract_siem_evidence,
    reason_blast_radius,
    reason_verify,
    reason_why,
)
from workers.os_diagnostic_loop import run_os_diagnostic_loop
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
from workers.diagnostic_k8s_clinical import confirm_workload_unavailable, resolve_workload_from_pod
from workers.baseline_snapshot import REDIS_KEY_SNAPSHOT, REDIS_KEY_TS
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
from workers.telegram_escalation import (
    emit_telegram_escalation,
    format_operator_action_card,
    format_operator_triage_card,
)
from workers.request_trace import pop_trace_id, push_trace_id
from workers.remote_agent_pipeline import handle_discovery_evidence, handle_remote_agent_evidence
from workers.alert_envelope import ALERT_KIND_META_SELF
from workers.pipeline_stages import mark_stage
from workers.telegram_outbound import send_telegram_out_for_inbound
from workers.telegram_advisory_emitter import (
    _e,
    copy_advisory_for_telegram_if_mismatch,
    render_advisory_to_telegram,
)
from workers.unified_incident_card import (
    AuditMeta,
    UnifiedCard,
    esc as _uc_esc,
    render_audit_footer,
    render_unified_card,
)


def _contrast_audit_footer(trace: str, settings: Any) -> str:
    """Shared plain-text audit footer for deterministic contrast (false-alarm) digests."""
    return "\n\n" + render_audit_footer(
        str(trace),
        AuditMeta(
            mode=getattr(settings, "omni_autonomy_tier", None),
            decision="SUGGEST",
            origin="deterministic",
            action="Đã chặn mutate — cảnh báo không khớp trạng thái thực (read-before-mutate)",
            crat_event="ADVISORY_DISPATCHED",
        ),
        markdown=False,
    )
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
from services.audit_ledger.chain_writer import write_audit_block
from services.audit_ledger.signer import AuditLedgerError

logger = logging.getLogger(__name__)

# Log fragment injected into analyst evidence when baseline z-scores exist (E2E / log greppers).
SIGMA_RESOURCE_EVIDENCE_BASELINE_MARKER = "3-SIGMA RESOURCE BASELINE"

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


def _tenant_id_from_batch(batch: list[dict[str, Any]]) -> str:
    """First non-empty ``tenant_id`` in the batch, else the lab default.

    ``tenant_id`` is threaded by the agent's own config (never LLM-inferred) —
    see ``coerce_evidence_dict`` — onboarding-ops-agent plan, step 1.
    """
    for b in batch or ():
        tid = str(b.get("tenant_id") or "").strip()
        if tid:
            return tid
    return "default"


_SIEM_CATEGORY_STEPS: dict[str, list[str]] = {
    "ddos": [
        "kubectl get networkpolicy -n {ns}",
        "kubectl get ingress -n {ns}",
        "kubectl get hpa -n {ns}",
        "Rà soát cấu hình rate-limit WAF/ingress; chặn IP affected_ip={ip}",
    ],
    "malware": [
        "kubectl get pods -n {ns} --show-labels",
        "kubectl get pods -n {ns} -o wide | grep {ip}",
        "kubectl get pods -n {ns} -o jsonpath='{{range .items[*]}}{{.metadata.name}}{{\"\\n\"}}{{end}}' | xargs -I{{}} kubectl describe pod {{}} -n {ns}",
        "Cô lập: kubectl cordon <node-chứa-pod-khả-nghi>; thu thập forensics trước khi xoá",
    ],
    "data_exfil": [
        "kubectl get networkpolicy -n {ns}",
        "kubectl get rolebindings,clusterrolebindings -n {ns}",
        "kubectl get pods -n {ns} -o wide | grep {ip}",
        "Soát lưu lượng egress và RBAC; thu hồi các role quá quyền",
    ],
    "k8s_threat": [
        "kubectl get clusterrolebindings | grep -i admin",
        "kubectl get pods -n {ns} -o wide --show-labels",
        "kubectl get pods -n {ns} -o json | grep -E 'hostPID|privileged|hostNetwork'",
        "Kiểm tra leo thang đặc quyền; rà RBAC và securityContext của pod",
    ],
    "auth_failure": [
        "kubectl logs -n {ns} -l app=auth --tail=200",
        "kubectl get serviceaccounts -n {ns}",
        "kubectl get rolebindings -n {ns}",
        "Xoay vòng credential; soát token service-account",
    ],
    "lateral_movement": [
        "kubectl get networkpolicy -n {ns}",
        "kubectl get pods -n {ns} -o wide | grep {ip}",
        "kubectl get pods -n {ns} --show-labels",
        "Phân đoạn namespace: áp network policy deny-all; điều tra lưu lượng pod-tới-pod",
    ],
}
_SIEM_DEFAULT_STEPS = [
    "kubectl get pods -n {ns} --show-labels",
    "kubectl get events -n {ns} --sort-by=.lastTimestamp",
    "kubectl get pods -n {ns} -o wide | grep {ip}",
    "Áp khắc phục từ suggested_action bên dưới; xác minh bằng kubectl rollout status",
]

# SIEM forecast by (category, severity) — heuristic kill-chain timeline
# Keys: "critical"/"high"/"medium"/"low"; default to critical if not found
_SIEM_FORECAST: dict[str, dict[str, list[tuple[str, str, str, str]]]] = {
    # (timeframe, severity_level, prediction, confidence)
    "ddos": {
        "critical": [
            ("1h", "critical", "Throughput dịch vụ giảm >70%; HPA có thể không scale kịp.", "high"),
            ("3h", "catastrophic", "Dịch vụ ngừng hoàn toàn; dự kiến lỗi dây chuyền xuống hạ nguồn.", "high"),
            ("6h", "catastrophic", "Cạn kiệt hạ tầng; bão hoà connection pool database.", "medium"),
            ("12h", "catastrophic", "Ảnh hưởng đa vùng nếu chưa kích hoạt CDN/WAF.", "medium"),
            ("24h", "catastrophic", "Mất dịch vụ toàn bộ nếu không khắc phục bằng WAF/chặn IP.", "low"),
        ],
    },
    "malware": {
        "critical": [
            ("1h", "critical", "Malware bắt đầu di chuyển ngang; các pod lân cận có nguy cơ.", "high"),
            ("3h", "catastrophic", "Kênh exfiltration C2 được thiết lập; rò rỉ dữ liệu đang diễn ra.", "high"),
            ("6h", "catastrophic", "Cài cơ chế bám trụ (persistence); nguy cơ xâm nhập toàn cụm.", "medium"),
            ("12h", "catastrophic", "Vượt cửa sổ vi phạm quy định (đồng hồ GDPR 72h bắt đầu).", "high"),
            ("24h", "catastrophic", "Sự cố không kiểm soát được nếu không cô lập forensic toàn bộ.", "medium"),
        ],
    },
    "data_exfil": {
        "critical": [
            ("1h", "critical", "Exfiltration đang diễn ra; dữ liệu đã lộ một phần.", "high"),
            ("3h", "catastrophic", "Exfiltration hoàn tất; bắt buộc thông báo vi phạm.", "high"),
            ("6h", "catastrophic", "Kẻ tấn công xoá dấu vết; nguy cơ giả mạo log.", "medium"),
            ("12h", "catastrophic", "Vượt cửa sổ vi phạm tuân thủ.", "high"),
            ("24h", "catastrophic", "Kẻ tấn công có thể đã phá huỷ bằng chứng forensic.", "low"),
        ],
    },
    "k8s_threat": {
        "critical": [
            ("1h", "critical", "Pod đặc quyền có thể giành quyền cấp node.", "high"),
            ("3h", "catastrophic", "Xâm nhập cấp cluster-admin; mọi namespace có nguy cơ.", "high"),
            ("6h", "catastrophic", "Xâm nhập chuỗi cung ứng; có thể giả mạo image.", "medium"),
            ("12h", "catastrophic", "Chiếm toàn cụm; dữ liệu và pipeline CI/CD có nguy cơ.", "medium"),
            ("24h", "catastrophic", "Không thể phục hồi nếu không dựng lại cụm.", "low"),
        ],
    },
    "auth_failure": {
        "critical": [
            ("1h", "degraded", "Brute-force tiếp diễn; khoá tài khoản có thể ảnh hưởng người dùng hợp lệ.", "high"),
            ("3h", "critical", "Credential sắp bị xâm nhập nếu không áp rate-limit.", "high"),
            ("6h", "critical", "Khả năng chiếm tài khoản cao; cần xoay vòng token.", "medium"),
            ("12h", "catastrophic", "Credential bị xâm nhập dùng để truy cập ngang.", "medium"),
            ("24h", "catastrophic", "Vi phạm toàn diện nếu không bật multi-factor.", "low"),
        ],
        "high": [
            ("1h", "degraded", "Auth tiếp tục thất bại; có thể suy giảm dịch vụ.", "high"),
            ("3h", "degraded", "Nguy cơ khoá credential với người dùng hợp lệ.", "medium"),
            ("6h", "critical", "Leo thang nếu kẻ tấn công đổi credential.", "medium"),
            ("12h", "critical", "Tấn công kéo dài; nguy cơ xâm nhập credential.", "low"),
            ("24h", "critical", "Chiếm tài khoản nếu không khắc phục.", "low"),
        ],
    },
    "lateral_movement": {
        "critical": [
            ("1h", "critical", "Kẻ tấn công nhảy giữa các pod; vi phạm network policy.", "high"),
            ("3h", "catastrophic", "Nhiều namespace bị xâm nhập; secret có nguy cơ.", "high"),
            ("6h", "catastrophic", "Kẻ tấn công giành quyền control plane.", "medium"),
            ("12h", "catastrophic", "Xâm nhập ngang toàn cụm.", "medium"),
            ("24h", "catastrophic", "Sự cố không kiểm soát được; cần dựng lại toàn bộ.", "low"),
        ],
    },
    "network_anomaly": {
        "critical": [
            ("1h", "degraded", "Throughput mạng suy giảm; độ trễ dịch vụ tăng.", "high"),
            ("3h", "critical", "Suy giảm dịch vụ lan tới các microservice phụ thuộc.", "medium"),
            ("6h", "critical", "Nguy cơ phân mảnh mạng nếu bất thường liên quan định tuyến.", "medium"),
            ("12h", "catastrophic", "Service mesh sập hoàn toàn nếu không sửa định tuyến.", "low"),
            ("24h", "catastrophic", "Lỗi mạng cấp hạ tầng.", "low"),
        ],
    },
}

_SIEM_DEFAULT_FORECAST: list[tuple[str, str, str, str]] = [
    ("1h", "degraded", "Tác động sự cố bảo mật lan rộng; mối đe doạ chưa được khống chế.", "medium"),
    ("3h", "critical", "Khả năng leo thang nếu không khống chế.", "medium"),
    ("6h", "critical", "Tấn công kéo dài; nguy cơ di chuyển ngang hoặc lộ dữ liệu.", "low"),
    ("12h", "catastrophic", "Tác động toàn diện nếu không xử lý.", "low"),
    ("24h", "catastrophic", "Vi phạm quy định và vận hành nếu không khắc phục.", "low"),
]


def _siem_forecast_timeline(category: str, severity: str) -> list[dict[str, str]]:
    """Return heuristic kill-chain forecast for SIEM category × severity."""
    cat_map = _SIEM_FORECAST.get(category, {})
    timeline = cat_map.get(severity) or cat_map.get("critical") or _SIEM_DEFAULT_FORECAST
    return [
        {"timeframe": tf, "severity": sev, "prediction": pred, "confidence": conf}
        for tf, sev, pred, conf in timeline
    ]


def _format_siem_forecast_text(forecast: list[dict[str, str]]) -> str:
    """Format forecast timeline as operator-readable text."""
    lines = ["Dự báo (nếu không xử lý):"]
    for f in forecast:
        tf = f["timeframe"]
        sev = f["severity"].upper()
        pred = f["prediction"]
        conf = f["confidence"]
        lines.append(f"  +{tf}: [{sev}] {pred} (độ tin cậy={conf})")
    return "\n".join(lines)


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
    # Use explicit replacement instead of str.format() to prevent format-string injection
    # from untrusted SIEM-sourced ns/ip values containing "{...}" sequences.
    _safe_ns = str(ns or "").replace("{", "").replace("}", "")
    _safe_ip = str(affected_ip or "?").replace("{", "").replace("}", "")
    steps = [s.replace("{ns}", _safe_ns).replace("{ip}", _safe_ip) for s in raw_steps]
    steps_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps))

    # Structured WHAT / WHO / WHY / VERIFY / HOW-TO / FORECAST
    what = f"[{category.upper()}] {description or suggested or 'Security incident detected by FinGuard SIEM.'}"
    who = f"namespace={_safe_ns}" + (f", tenant={tenant}" if tenant else "") + (f", source_ip={_safe_ip}" if affected_ip else "")

    # WHY / VERIFY are reasoned from the incident *evidence* via the principle
    # engine (origin class + source cardinality + confirm-ingress), not from a
    # per-category lookup. A never-before-seen category or IP is still placed
    # correctly — no "not in the table -> empty default" dead-end. See
    # siem_reasoning.py + memory project_rag_kb_and_scope_prompt.
    _ev = extract_siem_evidence(batch, siem_labels)
    why = reason_why(_ev)
    blast = reason_blast_radius(_ev)
    verify = reason_verify(_ev)
    verify_text = "\n".join(f"{i+1}. {v}" for i, v in enumerate(verify))

    forecast_items = _siem_forecast_timeline(category, severity)
    forecast_text = _format_siem_forecast_text(forecast_items)

    return (
        f"WHAT: {what}\n"
        f"WHO: {who} | incident={incident_id} | severity={severity}\n"
        f"WHY: {why}\n"
        f"{blast}\n\n"
        f"VERIFY FIRST (xác nhận phạm vi trước khi hành động — các bước này có thể vô hiệu hoá phần bên dưới):\n"
        f"{verify_text}\n\n"
        f"HOW-TO (CHỈ khi VERIFY xác nhận cụm nằm trong phạm vi) cho [{category}] tại namespace [{_safe_ns}]:\n"
        f"{steps_text}\n\n"
        f"{forecast_text}\n\n"
        "Omni does NOT auto-execute — sự cố SIEM cần người phê duyệt."
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
    incident_id = siem.get("siem_incident_id", "n/a")
    severity = siem.get("severity", "n/a")
    category = siem.get("siem_category", "n/a")
    ns = siem.get("namespace") or "?"
    alert_name = siem.get("alertname") or f"SIEM{category.title().replace('_','')}"
    affected_ip = ""
    description = ""
    suggested_action = siem.get("suggested_action", "")
    tenant = ""
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
            affected_ip = _ef.get("affected_ip") or ""
            description = (_ef.get("description") or "")[:200]
            if _ef.get("tenant") and _ef["tenant"] != "unknown":
                tenant = _ef["tenant"]
            break
    problem = f"{alert_name} [{severity}] — incident={incident_id}"
    if ns and ns != "?":
        problem += f" ns={ns}"
    if tenant:
        problem += f" tenant={tenant}"
    if affected_ip:
        problem += f" ip={affected_ip}"
    reason = description or diagnosis[:300].strip() or f"category={category} — LLM diagnosis absent"
    chain_items: list[str] = []
    for b in (batch or [])[:5]:
        probe = str(b.get("probe") or "").strip()
        lane = str(b.get("lane") or b.get("evidence_source") or "").strip()
        hint = str(b.get("alert_hint") or b.get("result") or "").strip()[:80]
        ts = str(b.get("ts") or b.get("timestamp") or "").strip()[:19]
        bits = [x for x in (ts, f"[{lane}]" if lane else "", probe, f"— {hint}" if hint else "") if x]
        if bits:
            chain_items.append(" ".join(bits))
    # Extract WHY from structured diagnosis
    why_text = ""
    for ln in diagnosis.splitlines():
        if ln.startswith("WHY:"):
            why_text = ln[4:].strip()
            break
    reason = why_text or description or diagnosis[:300].strip() or f"category={category} — LLM diagnosis absent"

    advise: list[str] = []
    in_howto = False
    for ln in diagnosis.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("HOW-TO"):
            in_howto = True
            continue
        if in_howto:
            # Stop at forecast section
            if s.startswith(("Forecast", "Dự báo", "Omni does NOT", "Omni KHÔNG")):
                in_howto = False
                continue
            if s[0].isdigit() or s.startswith(("kubectl", "Review", "Apply", "Isolate", "Rotate", "Segment", "Audit", "Check", "Rà", "Cô lập", "Soát", "Kiểm tra", "Xoay", "Phân đoạn", "Áp")):
                advise.append(s)
    if not advise:
        # Fallback: parse old-style diagnosis
        for ln in diagnosis.splitlines():
            s = ln.strip()
            if s and (s[0].isdigit() or s.startswith(("kubectl", "Review", "Apply", "Isolate", "Rotate", "Segment", "Audit", "Check", "Rà", "Cô lập", "Soát", "Kiểm tra", "Xoay", "Phân đoạn", "Áp"))):
                advise.append(s)
    if suggested_action:
        advise.append(f"Khuyến nghị SIEM: {suggested_action[:200]}")
    if not advise:
        advise.append(f"Rà soát: {diagnosis[:400]}")
    advise.append("Omni KHÔNG tự thực thi sự cố SIEM — cần người phê duyệt")

    # Build forecast section for the unified card.
    forecast_items = _siem_forecast_timeline(category, severity)
    forecast_tuples = tuple(
        (str(f["timeframe"]), str(f["severity"]), _uc_esc(str(f["prediction"])))
        for f in forecast_items[:5]
    )

    # WHERE (workload/ns/tenant/ip) — drop the HITL boilerplate from advise into the audit footer.
    where_bits = [f"ns={ns}" if ns and ns != "?" else ""]
    if tenant:
        where_bits.append(f"tenant={tenant}")
    if affected_ip:
        where_bits.append(f"ip={affected_ip}")
    where_str = " · ".join(b for b in where_bits if b) or "cụm (chưa xác định)"
    how_to_lines = tuple(
        _uc_esc(a) for a in advise if not a.startswith("Omni KHÔNG")
    )
    verdict = "CONFIRMED" if str(severity).lower() in ("high", "critical") else "INVESTIGATE"
    card = UnifiedCard(
        lane="siem",
        verdict=verdict,
        title=f"{alert_name} [{severity}] — {category} (incident={incident_id})",
        trace_id=trace,
        what=reason,
        where=where_str,
        why=tuple(_uc_esc(c) for c in chain_items),
        how_to=how_to_lines,
        forecast=forecast_tuples,
        audit=AuditMeta(
            mode=getattr(ctx.settings, "omni_autonomy_tier", None),
            decision="HITL",
            origin="deterministic_siem",
            action="Omni KHÔNG tự xử lý sự cố SIEM — cần người phê duyệt",
            crat_event="SIEM_INCIDENT",
        ),
    )
    msg = render_unified_card(card)[:4096]
    try:
        await tg.send_message(int(admin_cid), msg, parse_mode="Markdown")
        logger.info(
            "event=siem_suggest_telegram_sent trace=%s siem_incident=%s",
            trace, siem.get("siem_incident_id", ""),
        )
    except Exception as e:
        logger.warning("event=siem_suggest_telegram_error trace=%s err=%s", trace, e)


def _rag_search_failed(detail: Any) -> bool:
    """RAG/embed/pgvector failed (400/500) — use fact-only SDK reasoning."""
    return isinstance(detail, dict) and str(detail.get("reason") or "") == "search_error"


def _extract_alert_ctx(batch: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract alert context fields from the first evidence item in batch."""
    if not batch:
        return {}
    b0 = batch[0]
    # canonical_query_snippet may carry JSON-encoded alert metadata
    raw_q = b0.get("canonical_query_snippet") or ""
    parsed_q: dict[str, Any] = {}
    if isinstance(raw_q, str) and raw_q.strip().startswith("{"):
        try:
            parsed_q = json.loads(raw_q) if isinstance(json.loads(raw_q), dict) else {}
        except Exception:
            pass
    return {
        "alertname": b0.get("alert_rule") or parsed_q.get("alertname") or "",
        "namespace": b0.get("namespace") or parsed_q.get("namespace") or "",
        "source": b0.get("evidence_source") or b0.get("source") or "",
        "labels": parsed_q.get("labels") or {},
        "annotations": parsed_q.get("annotations") or {},
    }


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
    extra["business_error_class"] = res.dominant_error_class
    if res.ok:
        logger.info(
            "event=log_surge_sigma_bypass_ok trace=%s reason=%s lines=%s error_class=%s",
            trace,
            res.reason,
            extra.get("lines_fetched"),
            res.dominant_error_class,
        )
        return True, {"log_surge_bypass": True, **extra}, False
    # 499 client_abort: informational — do not bypass sigma but log for operator
    if res.dominant_error_class == "client_abort":
        logger.info(
            "event=log_surge_client_abort_informational trace=%s lines=%s",
            trace,
            extra.get("lines_fetched"),
        )
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
    # Plan step 6 — meta/self-KPI alerts have no remediable cluster target. If the
    # ingestion classifier flagged this trace as meta_self, hard-close the gate so
    # the mutate-planner never anchors a mutation on Omni's own health signal.
    try:
        _ac_raw = await ctx.redis.get(f"omni:trace:{trace}:alert_class")
        if _ac_raw:
            _ac = json.loads(_ac_raw.decode() if isinstance(_ac_raw, bytes) else _ac_raw)
            if _ac.get("kind") == "meta_self":
                logger.info(
                    "event=proof_of_fault_meta_self_blocked trace=%s — no cluster target", trace
                )
                return (
                    False,
                    "ERR_META_SELF_NO_TARGET",
                    {"critical_evidence": False, "alert_class": "meta_self"},
                )
    except Exception:
        logger.debug("event=alert_class_read_skip trace=%s", trace, exc_info=True)

    critical = critical_evidence_present(batch)
    snap_raw = await ctx.redis.get(REDIS_KEY_SNAPSHOT)
    snap: dict[str, Any] = {}
    if snap_raw:
        try:
            snap = json.loads(snap_raw.decode() if isinstance(snap_raw, bytes) else snap_raw)
        except Exception as _je:
            logger.warning("event=baseline_snapshot_corrupt trace=%s err=%r raw_len=%d",
                           trace, _je, len(snap_raw) if snap_raw else 0)
            snap = {}
    ts_raw = await ctx.redis.get(REDIS_KEY_TS)
    if ts_raw:
        import time as _time
        _snap_age = _time.time() - float(ts_raw)
        if _snap_age > 300:
            logger.warning("event=baseline_snapshot_stale age_sec=%.0f trace=%s", _snap_age, trace)
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
        # Workload-availability faults (KubePodNotReady / replicas-mismatch) carry no
        # sigma or alert-side critical evidence — the alert claim alone is NOT proof.
        # Confirm via a LIVE read-only ground-truth check of the target's readiness.
        # Only a genuinely Ready=False workload yields physical proof; a healthy pod
        # keeps the gate closed (anti-hallucinate preserved).
        if workload_fault_incident_rollout_eligible(batch):
            ident = _identity_from_batch(batch)
            ns_c = str(ident.get("namespace") or "").strip()
            pod_c = str(ident.get("pod") or "").strip()
            if ns_c and pod_c and await confirm_workload_unavailable(ns_c, pod_c):
                critical = True
                meta["critical_evidence"] = True
                meta["physical_proof_source"] = "live_workload_unavailable_confirmed"
                lane = "state"
                meta["proof_lane"] = "state"
                meta["proof_lane_source"] = "live_state_confirm"
                logger.warning(
                    "event=proof_of_fault_live_state_confirmed trace=%s ns=%s pod=%s",
                    trace, ns_c, pod_c,
                )
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
    audit: bool = True,
) -> None:
    if not ctx.settings.trace_correlation_ping_enabled:
        return
    k = ctx.kafka
    if k is None:
        return
    tid = str(trace or "").strip()
    if not tid:
        return
    # CRAT Fail-Closed (INV): an advisory dispatch is an auditable action — the
    # audit block MUST be written before ANY action emit. Callers that already
    # wrote their own ADVISORY_DISPATCHED block (SIEM / contrast / LLM-analyst)
    # pass audit=False; every other dispatch path is covered here by default so
    # no early-exit can emit a suggestion without an audit trail.
    if audit:
        try:
            await write_audit_block(
                event_type="ADVISORY_DISPATCHED",
                trace_id=tid,
                payload={
                    "source": source,
                    "lane": lane or "",
                    "suggested_tool": suggested_tool,
                    "diagnosis": str(diagnosis)[:2000],
                },
                redis=ctx.redis,
                kafka=ctx.kafka,
                kafka_topic=ctx.settings.kafka_topic_audit_chain,
            )
        except AuditLedgerError as _audit_err:
            logger.critical(
                "event=crat_fail_closed_abort trace=%s source=%s err=%r — suggest dispatch aborted",
                tid, source, _audit_err,
            )
            await mark_stage(ctx.redis, tid, "CRAT", "fail", detail="audit_chain_write_failed")
            return
        await mark_stage(ctx.redis, tid, "CRAT", "ok", detail="ADVISORY_DISPATCHED")
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
            audit=False,  # already audited above in _emit_suggest_remediation
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
        tenant_id=getattr(ctx, "current_tenant_id", None),
    )
    try:
        await k.send_dict(ctx.settings.kafka_topic_actions, {"data": json.dumps(body, ensure_ascii=False)})
        logger.info(
            "event=action_emitted action=SUGGEST_REMEDIATION trace=%s source=%s",
            tid,
            source,
        )
        await mark_stage(ctx.redis, tid, "DISPATCH", "ok", detail="SUGGEST_REMEDIATION")
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
    audit: bool = True,
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
    # CRAT Fail-Closed: a runbook dispatch is an auditable action. The delegating
    # _emit_suggest_remediation path already audits (passes audit=False); direct
    # callers are covered here so no runbook emit escapes the audit trail.
    if audit:
        try:
            await write_audit_block(
                event_type="ADVISORY_DISPATCHED",
                trace_id=tid,
                payload={"source": source, "mode": "os_runbook", "diagnosis": str(diagnosis)[:2000]},
                redis=ctx.redis,
                kafka=ctx.kafka,
                kafka_topic=ctx.settings.kafka_topic_audit_chain,
            )
        except AuditLedgerError as _audit_err:
            logger.critical(
                "event=crat_fail_closed_abort trace=%s source=%s mode=os_runbook err=%r — dispatch aborted",
                tid, source, _audit_err,
            )
            await mark_stage(ctx.redis, tid, "CRAT", "fail", detail="audit_chain_write_failed")
            return False
        await mark_stage(ctx.redis, tid, "CRAT", "ok", detail="ADVISORY_DISPATCHED")
    body = build_suggest_os_runbook_body(
        tid,
        diagnosis=diagnosis,
        confidence=confidence,
        source=source,
        runbook_title=runbook_title,
        commands=commands,
        reasoning_chain=reasoning_chain,
        verification_evidence_digest=verification_evidence_digest,
        tenant_id=getattr(ctx, "current_tenant_id", None),
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


async def _resolve_rollout_args(
    ctx: WorkerHandlerContext, batch: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Resolve {namespace, deployment} for a rollout restart.

    Prefer the alert labels (fast, no API call). When the labels lack a
    deployment/workload — common for KubePodNotReady / KubePodCrashLooping which
    carry only namespace+pod — fall back to resolving the pod's top controller via
    OwnerReferences (read-only). This closes F22-residual: the deterministic rollout
    safety-net previously fired inconsistently because label-only resolution returned
    None whenever the alert had no deployment label.
    """
    direct = rollout_args_from_evidence_batch(batch)
    if direct:
        return direct
    ident = _identity_from_batch(batch)
    ns = ident.get("namespace") or ""
    pod = ident.get("pod") or ""
    if not ns or not pod:
        return None
    ctl = await resolve_workload_from_pod(ns, pod)
    if not ctl:
        return None
    _kind, name = ctl
    if not name:
        return None
    return {"namespace": ns, "deployment": name}


async def _synthesize_rollout_safety_net_plan(
    ctx: WorkerHandlerContext, batch: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Build a deterministic k8s_rollout_restart plan for a KNOWN rollout-eligible fault.

    Returns None when the fault is not rollout-eligible or no target can be resolved.
    The returned plan carries planner_origin="deterministic_safety_net_hoisted" so it
    flows through every mutate gate (proof-of-fault, INV_*, tier, namespace, kill-switch,
    snapshot, post-mutate reconcile→rollback) while bypassing only the LLM precondition
    re-ask gate. It does NOT depend on the flaky agentic planner converging.
    """
    if not (
        workload_fault_incident_rollout_eligible(batch)
        or workload_cpu_incident_rollout_eligible(batch)
    ):
        return None
    rr = await _resolve_rollout_args(ctx, batch)
    if not rr:
        return None
    return {
        "tool_name": "k8s_rollout_restart",
        "args": {"namespace": rr["namespace"], "deployment": rr["deployment"]},
        "discovery_steps": [],
        "lane_hint": "state",
        "reasoning_chain": None,
        "planner_origin": "deterministic_safety_net_hoisted",
    }


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
    forced_plan: dict[str, Any] | None = None,
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
    unrestricted = bool(getattr(ctx.settings, "omni_unrestricted_tool_execution", False))
    legacy_det_fallback = bool(getattr(ctx.settings, "omni_legacy_deterministic_fallback", False))
    # In LLM-first mode, force planner-led diagnose→verify→mutate flow.
    # "unrestricted" controls execution gates, not whether deterministic shortcuts bypass planner.
    allow_det = legacy_det_fallback and not llm_first
    blind_pre = await infer_blind_proof_lane_hint(
        ctx, batch, sanitized_text=sanitized_text, rag_match_text=rag_match_text
    )
    initial_symptom = initial_symptom_from_evidence_batch(batch)
    det_plan = None
    discovery_steps: list[str] = []
    if forced_plan is not None:
        # Hoisted deterministic plan (known rollout-eligible fault). Skip the flaky LLM
        # planner entirely; flow straight into the same mutate gates below. F22-residual.
        plan = dict(forced_plan)
        discovery_steps = list(forced_plan.get("discovery_steps") or [])
        logger.warning(
            "event=agentic_mutate_forced_plan trace=%s tool=%s origin=%s",
            trace,
            str(plan.get("tool_name") or ""),
            str(plan.get("planner_origin") or ""),
        )
    elif allow_det:
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
        discovery_steps = list(det_plan.get("discovery_steps") or [])
    elif forced_plan is None:
        lane_for_mx, _ls = resolve_proof_lane(batch, rag_match_text=rag_match_text)
        mx = int(getattr(ctx.settings, "autonomous_agentic_max_steps", 5) or 5)
        if lane_for_mx == "state":
            mx = max(mx, 8)
        # Recall similar verified playbooks from pgvector (advisory only; no secret values injected).
        recall_result = await recall_playbook_advisory(
            ctx, query_text=sanitized_text, trace=trace, tenant_id=_tenant_id_from_batch(batch)
        )
        # Pipeline stage: RAG — mark as skip if strong hit (LLM would be bypassed), else ok.
        if recall_result and recall_result.top_score is not None and recall_result.top_score >= 0.75:
            await mark_stage(ctx.redis, trace, "RAG", "skip", detail=f"recall={recall_result.top_score:.3f}")
        else:
            _rag_score = recall_result.top_score if recall_result else 0.0
            await mark_stage(ctx.redis, trace, "RAG", "ok", detail=f"recall={_rag_score:.3f}" if _rag_score else "no_hit")
        # S2.4: persist point_id so feedback loop can downvote on failure.
        if recall_result and recall_result.top_point_id:
            try:
                await ctx.redis.setex(
                    f"omni:recall:trace_point_id:{trace}",
                    7200,
                    recall_result.top_point_id,
                )
            except Exception:
                pass
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
    # F15-extra: qwen's most common failure mode is NOT "no plan" (None) but a tool-less
    # planner result — decision=escalate / schema-reject-streak abort / "can't assist" — which
    # is truthy and so slips PAST the `if not plan:` deterministic safety-net below, degrading a
    # KNOWN self-healable workload fault to SUGGEST. Normalize any tool-less planner result to
    # None here so the safety-net + suggest fallback treat it uniformly as "no mutate plan".
    # PHASE_DONE and CHANNEL_MISMATCH carry their own handled semantics and are preserved.
    if plan is not None:
        _rc_norm = str(plan.get("reason_code") or "")
        _tn_norm = str(plan.get("tool_name") or "").strip()
        # qwen non-convergence is polymorphic: tool-less (decision=escalate/prose), OR a
        # parseable-but-non-mutate terminal tool (read-only kubectl_cluster repeated with bad
        # args). Both must route to the deterministic safety-net, not block it. Normalize ANY
        # planner result that did NOT terminate on a valid MUTATE_TOOL_ALLOWLIST tool to None.
        # PHASE_DONE (resolved) and CHANNEL_MISMATCH (explicit read-only route) keep their own
        # handlers below and are preserved.
        if (
            _rc_norm not in (PLANNER_PHASE_DONE, ERR_SEM_CHANNEL_MISMATCH)
            and _tn_norm not in MUTATE_TOOL_ALLOWLIST
        ):
            logger.info(
                "event=planner_nonmutate_result_normalized_to_none trace=%s reason_code=%s tool=%s",
                trace, _rc_norm or "none", _tn_norm or "none",
            )
            plan = None
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
        # F15: the LLM agentic planner is flaky (emits read-only tools / "can't assist"
        # / schema violations) and can leave a KNOWN, self-healable fault with no plan —
        # degrading to SUGGEST even when a safe, reversible remediation is obvious. For
        # well-defined fault classes (workload fault / CPU saturation), synthesize a
        # deterministic k8s_rollout_restart so autonomy does not hinge on the LLM
        # producing valid JSON. This flows through the SAME downstream gates as any
        # mutate (proof-of-fault, INV_*, tier gate, namespace isolation, kill-switch,
        # pre-mutate snapshot, post-mutate reconcile→rollback). In fail-closed mode the
        # advisory kill-switch still routes it to SUGGEST — it only becomes a real mutate
        # when auto-execute is explicitly enabled.
        rr_sn = await _resolve_rollout_args(ctx, batch)
        if rr_sn and (
            workload_fault_incident_rollout_eligible(batch)
            or workload_cpu_incident_rollout_eligible(batch)
        ):
            plan = {
                "tool_name": "k8s_rollout_restart",
                "args": dict(rr_sn),
                "discovery_steps": discovery_steps,
                "lane_hint": None,
                "reasoning_chain": None,
                "planner_origin": "deterministic_safety_net_after_planner_fail",
            }
            logger.warning(
                "event=deterministic_safety_net_after_planner_fail trace=%s tool=k8s_rollout_restart",
                trace,
            )
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
    # The precondition gate validates LLM-planner output and RE-ASKS the (flaky) LLM when a
    # precondition (e.g. prior discovery) is missing. A deterministically synthesized plan
    # (origin "deterministic" / "deterministic_safety_net_after_planner_fail") carries trusted
    # args (namespace+deployment from the alert labels) for an already proof-of-fault-confirmed
    # workload fault — re-asking the LLM here only re-triggers the abort that produced the
    # safety-net in the first place. Bypass the gate for ALL deterministic origins; the real
    # safety gates (proof-of-fault, INV_NO_RESTART_ON_BROKEN_SPEC, tier, namespace) still run below.
    if (
        plan
        and bool(getattr(ctx.settings, "omni_planner_precondition_gate_enabled", True))
        and (not unrestricted)
        and not plan_origin.startswith("deterministic")
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
    readonly_discovery_executed: list[str] = []
    if bool(getattr(ctx.settings, "omni_discovery_mandatory", False)):
        mem_dm = await load_trace_memory(
            ctx.redis,
            trace,
            initial_symptoms="",
            initial_symptom=None,
        )
        readonly_discovery_executed = [
            str(a.tool_name)
            for a in (mem_dm.action_history or [])
            if str(getattr(a, "kind", "") or "") == "readonly_executed" and not bool(getattr(a, "is_error", False))
        ]
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
            readonly_discovery_executed=readonly_discovery_executed or None,
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
            except (asyncio.TimeoutError, OSError, Exception) as e:
                logger.error(
                    "event=mutation_aborted_missing_snapshot tool=%s ns=%s dep=%s err=%r trace=%s",
                    tn, ns, dep, e, trace,
                )
                return False
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
    # NOTE: SRE-Autonomous tier gate (shadow|minimal|autonomous) is enforced at the
    # EXECUTOR boundary (kafka_actions_consumer), NOT here — evidence_consumer only
    # emits EXECUTE_MUTATE *intent*. The plan's reasoning-source provenance travels in
    # the action payload (planner_origin) so the executor can apply the origin-aware
    # tier matrix. This keeps the plan→execute contract intact.
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
        # L4 playbook-first (dual-run): mutate plan đã qua đủ gate ở trên — nếu match
        # PlaybookSpec thì route EXECUTE_PLAYBOOK (engine có proof-of-fault/breaker/
        # graduation); không match/flag off → EXECUTE_MUTATE cũ nguyên trạng.
        from workers.autonomous_decider import _try_match_playbook
        from workers.playbook_emit import emit_execute_playbook

        pb_id = await _try_match_playbook(ctx, trace=trace, tool_name=tn, args=args)
        if pb_id:
            render_ctx_pb = {
                k: str(v) for k, v in args.items()
                if k in ("namespace", "deployment", "pod", "name") and v
            }
            enqueued = await emit_execute_playbook(
                ctx, trace=trace, playbook_id=pb_id, render_ctx=render_ctx_pb,
            )
        else:
            enqueued = await emit_execute_mutate(
                ctx,
                trace=trace,
                tool_name=tn,
                args=args,
                attempt_count=ac,
                reasoning_chain=exec_rc if isinstance(exec_rc, dict) else None,
                planner_origin=plan_origin,
            )
        if not enqueued:
            await emit_terminal_tombstone(
                ctx,
                trace_id=trace,
                reason_code="MUTATE_ENQUEUE_FAILED",
                component="evidence_consumer",
                detail=f"tool={tn} CRAT_fail_closed_or_kafka_unavailable",
            )
            await _emit_suggest_remediation(
                ctx,
                trace=trace,
                diagnosis=(
                    "EXECUTE_MUTATE was not enqueued: audit ledger write failed (fail-closed) "
                    "or Kafka publish failed. Check CRAT health and omni-actions availability; "
                    "apply remediation manually if appropriate."
                ),
                confidence=0.55,
                source="MUTATE_ENQUEUE_FAILED",
                suggested_tool="k8s_describe_resource",
            )
            await emit_telegram_escalation(
                ctx,
                trace,
                f"MUTATE_ENQUEUE_FAILED tool={tn} trace={trace}",
                reason="MUTATE_ENQUEUE_FAILED",
            )
            return False
    return True


async def _crat_for_deterministic_advisory(
    ctx: WorkerHandlerContext,
    *,
    trace: str,
    source: str,
    diagnosis: str,
) -> bool:
    """Write the ADVISORY_DISPATCHED audit block + mark the deterministic-path
    pipeline stages BEFORE any Telegram emit / action dispatch.

    The deterministic contrast paths (STATE_MACHINE_CONTRAST / OS_STATE_CONTRAST)
    bypass the LLM advisory block, so they must honour the CRAT fail-closed
    invariant themselves — exactly like the SIEM short-circuit. Returns False
    (caller MUST abort dispatch) if the audit write fails."""
    await mark_stage(ctx.redis, trace, "RAG", "skip", detail="deterministic contrast — no second-brain RAG")
    await mark_stage(ctx.redis, trace, "LLM", "skip", detail="deterministic contrast — no LLM")
    await mark_stage(ctx.redis, trace, "VERIFY", "skip", detail="no LLM advisory to verify (deterministic)")
    await mark_stage(ctx.redis, trace, "SCHEMA", "ok", detail=source)
    await mark_stage(ctx.redis, trace, "KILLSWITCH", "skip", detail="suggest-only — no mutate path")
    try:
        await write_audit_block(
            event_type="ADVISORY_DISPATCHED",
            trace_id=trace,
            payload={"source": source, "diagnosis": diagnosis[:2000], "mode": "deterministic_contrast"},
            redis=ctx.redis,
            kafka=ctx.kafka,
            kafka_topic=ctx.settings.kafka_topic_audit_chain,
        )
    except AuditLedgerError as _audit_err:
        logger.critical(
            "event=crat_fail_closed_abort trace=%s source=%s err=%r — dispatch aborted",
            trace, source, _audit_err,
        )
        await mark_stage(ctx.redis, trace, "CRAT", "fail", detail="audit_chain_write_failed")
        return False
    await mark_stage(ctx.redis, trace, "CRAT", "ok", detail="ADVISORY_DISPATCHED")
    return True


async def _handle_meta_self_alert(ctx: WorkerHandlerContext, *, trace: str) -> str | None:
    """Short-circuit self-monitoring/KPI alerts (OmniAdvisoryAcceptanceRateLow, ...).

    These have no remediable cluster target (alert_envelope.classify_alert already
    blocks mutation for them). Without this short-circuit every re-fire of the same
    underlying KPI alert was burning a full RAG+LLM cycle, escalating to Telegram,
    and dead-ending as a fresh DLQ tombstone every ~5min — none of it actionable.
    Returns a display string if this trace was handled here, else None to fall
    through to the normal diagnosis flow.
    """
    try:
        raw = await ctx.redis.get(f"omni:trace:{trace}:alert_class")
        if not raw:
            return None
        ac = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        return None
    if ac.get("kind") != ALERT_KIND_META_SELF:
        return None

    alertname = str(ac.get("alertname") or "unknown")
    cooldown_sec = int(getattr(ctx.settings, "meta_self_alert_cooldown_sec", 1800) or 1800)
    ck = f"omni:meta_self:cooldown:{alertname}"
    if await ctx.redis.get(ck):
        await mark_stage(ctx.redis, trace, "RAG", "skip", detail="meta_self — deduped, no RAG")
        await mark_stage(ctx.redis, trace, "LLM", "skip", detail="meta_self — deduped, no LLM")
        await mark_stage(
            ctx.redis, trace, "DISPATCH", "skip",
            detail=f"meta_self alert {alertname} suppressed — cooldown active",
        )
        await _mark_suggest_only_terminal(ctx, trace)
        logger.info("event=meta_self_alert_deduped trace=%s alertname=%s", trace, alertname)
        return "[META_SELF DEDUPED] self-monitoring alert suppressed within cooldown window"

    diagnosis = (
        f"Cảnh báo nội bộ Omni (self-monitoring/KPI): {alertname}. "
        "Không có cluster target để mutate — đây là tín hiệu sức khoẻ của chính Omni, "
        "không phải lỗi workload khách hàng. Xem dashboard /kpi để xác minh số liệu thật "
        "(VD: acceptance_rate có thể =0% do thiếu mẫu/cold-start, không hẳn là regressions)."
    )
    if not await _crat_for_deterministic_advisory(
        ctx, trace=trace, source="META_SELF_DETERMINISTIC", diagnosis=diagnosis,
    ):
        return "[ADVISORY MODE FAIL_CLOSED] meta_self audit_chain_write_failed — dispatch aborted"
    await _emit_suggest_remediation(
        ctx,
        trace=trace,
        diagnosis=diagnosis,
        confidence=1.0,
        source="META_SELF_DETERMINISTIC",
        suggested_tool="inspect_kpi_dashboard",
        audit=False,  # CRAT written by _crat_for_deterministic_advisory above
    )
    await _mark_suggest_only_terminal(ctx, trace)
    await ctx.redis.setex(ck, cooldown_sec, "1")
    logger.info("event=meta_self_alert_dispatched trace=%s alertname=%s cooldown=%ds", trace, alertname, cooldown_sec)
    return "[META_SELF] self-monitoring advisory dispatched (deterministic, no LLM)"


async def _mark_suggest_only_terminal(ctx: WorkerHandlerContext, trace: str) -> None:
    """Resolve the terminal stages for a suggest-only advisory: there is no HITL
    approval, executor mutation, or feedback loop on this path, so mark them skip
    instead of leaving them perpetually pending on the operator dashboard."""
    for _stage, _detail in (
        ("HITL", "suggest-only — no approval queue"),
        ("EXECUTOR", "suggest-only — no mutate"),
        ("FEEDBACK", "suggest-only — terminal"),
    ):
        await mark_stage(ctx.redis, trace, _stage, "skip", detail=_detail)


async def reason_from_diagnostic_evidence(ctx: WorkerHandlerContext, fields: dict[str, str]) -> str:
    """Evidence → batch → so alert vs state machine SDK (nếu mâu thuẫn rõ) → RagGate | LLM."""
    raw = fields.get("data") or "{}"
    try:
        ev_doc = json.loads(raw)
    except Exception:
        ev_doc = {"kind": "parse_error", "raw": raw[:8000]}
    ev_doc = coerce_evidence_dict(ev_doc)
    # Preserve tenant identity for every action envelope emitted later in this
    # diagnostic flow; autonomy policy is tenant-scoped at the executor.
    ctx.current_tenant_id = str(ev_doc.get("tenant_id") or ev_doc.get("tenant") or "").strip() or None
    trace = str(ev_doc.get("trace_id") or "evidence-unknown")

    # Remote agent evidence (all domains: os, database, services, storage, k8s, logs)
    # probe prefix routing via detect_domain() inside handle_remote_agent_evidence
    if ev_doc.get("evidence_source") == "RemoteAgent":
        tok = push_trace_id(trace)
        try:
            ctx.inbound_trace_id = trace
            return await handle_remote_agent_evidence(ctx, ev_doc, trace)
        finally:
            pop_trace_id(tok)

    # Direct database health-check evidence (ProxySQL admin, MySQL direct injection)
    # probe values: mysql_health, proxysql_stats, db_*
    if ev_doc.get("evidence_source") == "DirectDatabase":
        tok = push_trace_id(trace)
        try:
            ctx.inbound_trace_id = trace
            return await handle_remote_agent_evidence(ctx, ev_doc, trace)
        finally:
            pop_trace_id(tok)

    # Direct storage evidence (NFS mounts, disk health outside remote agent wrapper)
    # probe values: disk_usage, storage_nfs, disk_*, storage_*
    if ev_doc.get("evidence_source") == "DirectStorage":
        tok = push_trace_id(trace)
        try:
            ctx.inbound_trace_id = trace
            return await handle_remote_agent_evidence(ctx, ev_doc, trace)
        finally:
            pop_trace_id(tok)

    # Direct services evidence (HAProxy, systemd units outside remote agent wrapper)
    # probe values: service_haproxy, service_systemd_units
    if ev_doc.get("evidence_source") == "DirectServices":
        tok = push_trace_id(trace)
        try:
            ctx.inbound_trace_id = trace
            return await handle_remote_agent_evidence(ctx, ev_doc, trace)
        finally:
            pop_trace_id(tok)

    # Onboarding discovery evidence (process_list/port_scan/service_topology/doc
    # snapshot probes) — NOT a diagnostic alert, never touches K8s-specific
    # diagnosis logic below. Validated + forwarded to the onboarding worker.
    if ev_doc.get("evidence_source") == "DiscoveryEvidence":
        tok = push_trace_id(trace)
        try:
            ctx.inbound_trace_id = trace
            return await handle_discovery_evidence(ctx, ev_doc, trace)
        finally:
            pop_trace_id(tok)

    tok = push_trace_id(trace)
    try:
        ctx.inbound_trace_id = trace
        # Pipeline stage: evidence received — fire-and-forget, best-effort.
        # Domain do nguồn tự khai (nếu có) đi kèm ngay từ đây để trace có lĩnh vực
        # thật thay vì phải suy từ lane. Không chuẩn hoá được ⇒ để RỖNG, không ghi
        # "unknown": rỗng còn được lấp bởi call site sau (last-non-empty-wins),
        # còn "unknown" thì hiện lên UI như một nhãn có thật.
        _ev_domain = normalize_domain(ev_doc.get("domain"))
        # `signal_kind` do nguồn khai; chỉ nhận đúng hai giá trị hợp lệ, giá trị lạ ⇒
        # rỗng (last-non-empty-wins sẽ để call site sau lấp) chứ không ghi bừa.
        _ev_kind = str(ev_doc.get("signal_kind") or "")
        await mark_stage(
            ctx.redis, trace, "EVIDENCE", "ok",
            domain="" if _ev_domain == _DOMAIN_UNKNOWN else _ev_domain,
            signal_kind=_ev_kind if _ev_kind in ("diagnostic", "learning") else "",
        )
        # MTTD early registration: persist detection timestamp before analysis begins.
        # kpi_metrics.py reads this key to compute accurate MTTD (vs. receiving it from feedback).
        import time as _mttd_time
        try:
            await ctx.redis.set(f"omni:incident:ts:{trace}", str(_mttd_time.time()), ex=7200)
        except Exception as _mttd_err:
            logger.debug("event=mttd_ts_register_fail trace=%s err=%s", trace, _mttd_err)
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

        batch = await append_evidence_and_take_flush_batch(
            ctx.redis,
            trace,
            ev_doc,
            agg_timeout_sec=float(
                getattr(ctx.settings, "evidence_batch_agg_timeout_sec", 3.0) or 3.0
            ),
        )
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

        _meta_self_out = await _handle_meta_self_alert(ctx, trace=trace)
        if _meta_self_out is not None:
            return _meta_self_out

        try:
            from pkg.trace_orchestrator import (
                TraceOrchestratorPhase,
                TraceOrchestratorState,
                enqueue_rag_candidate,
                load_trace_orchestrator_state,
                save_trace_orchestrator_state,
            )

            orch = await load_trace_orchestrator_state(ctx.redis, trace)
            if orch is None:
                orch = TraceOrchestratorState(
                    trace_id=trace,
                    phase=TraceOrchestratorPhase.RAG_TRIALS,
                )
            if matched_playbook is not None:
                enqueue_rag_candidate(orch, f"playbook:{matched_playbook.playbook_id}")
            saved_orch = await save_trace_orchestrator_state(ctx.redis, orch)
            if not saved_orch:
                logger.warning(
                    "event=trace_orchestrator_persist_failed trace=%s — continuing without durable orch state",
                    trace,
                )
        except Exception as _orch_err:
            logger.debug("trace_orchestrator init trace=%s err=%s", trace, _orch_err)

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

        by_probe: dict[str, dict[str, Any]] = {}
        for _b in batch:
            _key = str(_b.get("probe") or "")
            if not _key:
                continue
            _existing = by_probe.get(_key)
            if _existing is None:
                by_probe[_key] = dict(_b)
            else:
                _ex_r = str(_existing.get("result") or "").upper()
                _new_r = str(_b.get("result") or "").upper()
                if _ex_r == "PASSED" and _new_r != "PASSED":
                    by_probe[_key] = dict(_b)
        contrast = compare_alert_claim_to_sdk_state(by_probe)
        if contrast is not None:
            contrast_st = contrast.strip()
            diagnosis_rich = build_contrast_diagnosis_for_action(by_probe, contrast_st)
            # CRAT fail-closed: audit MUST be written before the Telegram emit below.
            if not await _crat_for_deterministic_advisory(
                ctx, trace=trace, source="STATE_MACHINE_CONTRAST", diagnosis=diagnosis_rich,
            ):
                return "[ADVISORY MODE FAIL_CLOSED] state_machine_contrast audit_chain_write_failed — dispatch aborted"
            await _emit_suggest_remediation(
                ctx,
                trace=trace,
                diagnosis=diagnosis_rich,
                confidence=0.95,
                source="STATE_MACHINE_CONTRAST",
                suggested_tool="verify_metrics_alignment",
                audit=False,  # CRAT written by _crat_for_deterministic_advisory above
            )
            # Admin notify: structured plain text from probe fields (no LLM on this path).
            admin_cid = getattr(ctx.settings, "telegram_admin_chat_id", None)
            digest_loc = str(getattr(ctx.settings, "omni_operator_digest_locale", "both") or "both").lower()
            if digest_loc not in ("en", "vi", "both"):
                digest_loc = "both"
            if admin_cid and ctx.telegram:
                try:
                    t_msg = build_contrast_operator_telegram_body(
                        by_probe, contrast_st, str(trace), locale=digest_loc
                    )[:3700] + _contrast_audit_footer(trace, ctx.settings)
                    res = await ctx.telegram.send_message(
                        int(admin_cid),
                        t_msg[:3900],
                        parse_mode=None,
                    )
                    mid = (res.get("result") or {}).get("message_id")
                    logger.info(
                        "event=telegram_outbound_ok chat_id=%s message_id=%s trace=%s source=state_machine_contrast",
                        admin_cid,
                        mid,
                        trace,
                    )
                except Exception as te:
                    logger.warning("event=contrast_telegram_send_failed trace=%s err=%r", trace, te)
            if chat_id is not None:
                inbound_body = build_contrast_operator_telegram_body(
                    by_probe, contrast_st, str(trace), locale=digest_loc
                )[:3300] + _contrast_audit_footer(trace, ctx.settings)
                pld = {
                    "trace_id": trace,
                    "source": "diagnostic_evidence",
                    "text": inbound_body,
                    "diagnostic_evidence_sanitized": True,
                }
                await send_telegram_out_for_inbound(ctx, pld, trace, inbound_body)
            await emit_transition(
                ctx,
                trace_id=trace,
                transition=TRANSITION_PLAN_EMITTED,
                component="evidence_consumer",
                detail="state_machine_contrast_suggested",
            )
            await _mark_suggest_only_terminal(ctx, trace)
            return contrast

        # Lane 2 (state/SYS_HARD_FAIL): iterative OS diagnostic loop.
        # Guard: only run when batch is actually classified as lane=state to avoid
        # running OS probe checks on resource/app_log lanes where they don't apply.
        _pre_lane, _ = resolve_proof_lane(batch)
        _alert_ctx = _extract_alert_ctx(batch)
        os_contrast = (
            await run_os_diagnostic_loop(ctx, batch, by_probe, _alert_ctx, trace)
            if _pre_lane == "state" else None
        )
        if os_contrast is not None:
            os_contrast_st = os_contrast.strip()
            os_diagnosis_rich = build_contrast_diagnosis_for_action(by_probe, os_contrast_st)
            # CRAT fail-closed: audit MUST be written before the Telegram emit below.
            if not await _crat_for_deterministic_advisory(
                ctx, trace=trace, source="OS_STATE_CONTRAST", diagnosis=os_diagnosis_rich,
            ):
                return "[ADVISORY MODE FAIL_CLOSED] os_state_contrast audit_chain_write_failed — dispatch aborted"
            await _emit_suggest_remediation(
                ctx,
                trace=trace,
                diagnosis=os_diagnosis_rich,
                confidence=0.90,
                source="OS_STATE_CONTRAST",
                suggested_tool="k8s_describe_resource",
                audit=False,  # CRAT written by _crat_for_deterministic_advisory above
            )
            admin_cid = getattr(ctx.settings, "telegram_admin_chat_id", None)
            digest_loc = str(getattr(ctx.settings, "omni_operator_digest_locale", "both") or "both").lower()
            if digest_loc not in ("en", "vi", "both"):
                digest_loc = "both"
            if admin_cid and ctx.telegram:
                try:
                    t_msg = build_contrast_operator_telegram_body(
                        by_probe, os_contrast_st, str(trace), locale=digest_loc
                    )[:3700] + _contrast_audit_footer(trace, ctx.settings)
                    await ctx.telegram.send_message(int(admin_cid), t_msg[:3900], parse_mode=None)
                except Exception as _te:
                    logger.warning("event=os_contrast_telegram_failed trace=%s err=%r", trace, _te)
            await emit_transition(
                ctx,
                trace_id=trace,
                transition=TRANSITION_PLAN_EMITTED,
                component="evidence_consumer",
                detail="os_state_contrast_suggested",
            )
            await _mark_suggest_only_terminal(ctx, trace)
            return os_contrast

        # Lane 4 (SIEM): canonical DETERMINISTIC kill-chain card — runs for EVERY SIEM
        # batch, independent of omni_siem_suggest_only / omni_auto_execute_enabled.
        # Rationale (F25): a FinGuard SIEM incident (DDoS, network anomaly, exfil, auth
        # surge, …) has NO in-cluster workload to mutate — the K8s pod-mutate planner only
        # hallucinates fake pod targets (e.g. "nginx-ingress-controller-…", "nginx-pod")
        # for a DDoS from an external IP, then dead-ends to a schema-reject streak + DLQ.
        # Real SIEM remediation is firewall/NetworkPolicy (HIGH-risk → HITL via tier_gate),
        # never an autonomous pod mutate. The canonical, grounded output is the per-category
        # structured card (_siem_diagnosis_from_batch + principle-based WHY/VERIFY +
        # kill-chain forecast). Emit it directly, skip second-brain RAG + the agentic planner.
        # CRAT is written here (fail-closed) — the downstream _emit_suggest_remediation path
        # does NOT write its own audit block.
        if _is_siem_batch(batch):
            _siem = _siem_alert_labels(batch)
            _siem_text = format_batch_sanitized_analyst_user_text(batch)
            if len(batch) == 1:
                _siem_text = format_sanitized_analyst_user_text(batch[0])
            _siem_diag = _siem_diagnosis_from_batch(batch, _siem, _siem_text)
            await mark_stage(ctx.redis, trace, "RAG", "skip", detail="siem deterministic — no second-brain RAG")
            await mark_stage(ctx.redis, trace, "LLM", "skip", detail="siem kill-chain — deterministic, no LLM")
            await mark_stage(ctx.redis, trace, "VERIFY", "skip", detail="no LLM advisory to verify (deterministic)")
            await mark_stage(ctx.redis, trace, "SCHEMA", "ok", detail=f"siem_category={_siem.get('siem_category', 'unknown')}")
            await mark_stage(ctx.redis, trace, "KILLSWITCH", "skip", detail="siem — no in-cluster mutate path (firewall/NetworkPolicy is HITL)")
            # CRAT Fail-Closed Gate: audit write MUST succeed before any Telegram emit.
            try:
                await write_audit_block(
                    event_type="ADVISORY_DISPATCHED",
                    trace_id=trace,
                    payload={
                        "lane": "siem",
                        "siem_incident_id": _siem.get("siem_incident_id", ""),
                        "siem_category": _siem.get("siem_category", "unknown"),
                        "diagnosis": _siem_diag,
                        "source": "SIEM_DETERMINISTIC",
                    },
                    redis=ctx.redis,
                    kafka=ctx.kafka,
                    kafka_topic=ctx.settings.kafka_topic_audit_chain,
                )
            except AuditLedgerError as _siem_audit_err:
                logger.critical(
                    "event=audit_chain_write_failed phase=evidence_consumer lane=siem trace=%s err=%s FAIL_CLOSED",
                    trace, _siem_audit_err,
                )
                await mark_stage(ctx.redis, trace, "CRAT", "fail", detail="audit_chain_write_failed")
                return "[ADVISORY MODE FAIL_CLOSED] siem audit_chain_write_failed — dispatch aborted"
            await mark_stage(ctx.redis, trace, "CRAT", "ok", detail="ADVISORY_DISPATCHED")
            await _emit_suggest_remediation(
                ctx,
                trace=trace,
                diagnosis=_siem_diag,
                confidence=0.9,
                source="SIEM_DETERMINISTIC",
                suggested_tool="k8s_describe_resource",
                lane="siem",
                audit=False,  # CRAT written above (fail-closed) — do not double-write
            )
            await _notify_siem_telegram(ctx, trace=trace, batch=batch, diagnosis=_siem_diag)
            # SIEM remediation is firewall/NetworkPolicy (HIGH-risk → HITL), never an autonomous
            # in-cluster pod mutate. Mark terminal stages "skip" so operators don't see a
            # perpetually-pending pipeline (and so self-heal mode does not strand it at LLM-pending).
            for _term_stage, _term_detail in (
                ("HITL", "siem — firewall/NetworkPolicy remediation is out-of-band (SOC/HITL)"),
                ("EXECUTOR", "siem — no in-cluster mutate"),
                ("FEEDBACK", "siem — terminal"),
            ):
                await mark_stage(ctx.redis, trace, _term_stage, "skip", detail=_term_detail)
            await emit_transition(
                ctx,
                trace_id=trace,
                transition=TRANSITION_PLAN_EMITTED,
                component="evidence_consumer",
                detail="siem_deterministic",
                meta={"siem_incident_id": _siem.get("siem_incident_id", "")},
            )
            logger.info(
                "event=siem_deterministic_advisory_emitted trace=%s siem_incident=%s category=%s",
                trace, _siem.get("siem_incident_id", ""), _siem.get("siem_category", "unknown"),
            )
            return _siem_diag

        # **ADVISORY MODE INTEGRATION (Phase 5)**
        # Check if we should run advisory analyst instead of traditional flow
        if bool(getattr(ctx.settings, "omni_siem_suggest_only", True)) and not bool(
            getattr(ctx.settings, "omni_auto_execute_enabled", False)
        ):
            # Lane 1 (resource): 3σ gate must pass before advisory — alert may be wrong, sigma is ground truth.
            _adv_lane, _ = resolve_proof_lane(batch)
            if _adv_lane == "resource":
                _adv_snap_raw = await ctx.redis.get(REDIS_KEY_SNAPSHOT)
                _adv_snap_ts = await ctx.redis.get(REDIS_KEY_TS)
                _adv_no_ground_truth = ""
                if not _adv_snap_raw or not _adv_snap_ts:
                    _adv_no_ground_truth = "baseline snapshot chưa có"
                else:
                    import time as _time
                    _adv_snap_age = _time.time() - float(_adv_snap_ts)
                    if _adv_snap_age > 300:
                        _adv_no_ground_truth = f"baseline snapshot quá hạn ({_adv_snap_age:.0f}s > 300s)"

                # FAIL-CLOSED THẬT (sửa 2026-08-09). Trước đây chỗ này đặt
                # `_adv_snap_raw = None` rồi rơi xuống `if _adv_snap_raw:` — tức khối
                # chứa `return ""` bị bỏ qua và advisory VẪN CHẠY. Log ghi "fail closed"
                # nhưng hành vi là fail-OPEN, ngược hẳn. Đo tại P1 khớp chính xác:
                # `advisory_sigma_stale` 14 lần trên đúng 15 lần gọi LLM advisory — gần
                # như mọi advisory đều sinh ra từ lúc mất nền so sánh.
                # Với lane resource, 3σ baseline LÀ sự thật nền; không có nó thì không có
                # cơ sở nào để nói alert đúng hay sai, nên không được phép gọi LLM.
                # Suppression này CỐ Ý ồn ào (warning + 12 stage hiện trên dashboard) để
                # không biến thành im lặng ngược — nếu `baseline_snapshot_loop` chết, nó
                # phải đập vào mắt operator chứ không âm thầm nuốt mọi sự cố tài nguyên.
                if _adv_no_ground_truth:
                    logger.warning(
                        "event=advisory_sigma_no_ground_truth trace=%s reason=%s — fail closed, no advisory",
                        trace, _adv_no_ground_truth,
                    )
                    _gt_detail = f"3σ gate FAIL-CLOSED: {_adv_no_ground_truth} — không có nền so sánh, không gọi LLM"
                    await mark_stage(ctx.redis, trace, "RAG", "skip", detail="sigma_gate_no_ground_truth")
                    await mark_stage(ctx.redis, trace, "LLM", "skip", detail=_gt_detail)
                    await mark_stage(ctx.redis, trace, "SCHEMA", "skip", detail="no advisory (no ground truth)")
                    await mark_stage(ctx.redis, trace, "VERIFY", "skip", detail="no advisory to verify")
                    await mark_stage(ctx.redis, trace, "KILLSWITCH", "skip", detail="no dispatch (no ground truth)")
                    await mark_stage(ctx.redis, trace, "CRAT", "skip", detail="no dispatch — nothing to audit")
                    await mark_stage(ctx.redis, trace, "DISPATCH", "skip", detail="fail-closed — no alert sent")
                    await _mark_suggest_only_terminal(ctx, trace)
                    return ""
                if _adv_snap_raw:
                    try:
                        _adv_snap = json.loads(
                            _adv_snap_raw.decode() if isinstance(_adv_snap_raw, bytes) else _adv_snap_raw
                        )
                        _adv_z_thr = float(getattr(ctx.settings, "baseline_dr_z_threshold", 3.0) or 3.0)
                        _adv_z_cpu = _f64(_adv_snap.get("z_cpu"))
                        _adv_z_mem = _f64(_adv_snap.get("z_mem"))
                        _adv_dr = bool(_adv_snap.get("dr"))
                        _adv_sigma_ok = _adv_dr or bool(
                            (_adv_z_cpu is not None and abs(_adv_z_cpu) >= _adv_z_thr)
                            or (_adv_z_mem is not None and abs(_adv_z_mem) >= _adv_z_thr)
                        )
                        if not _adv_sigma_ok:
                            logger.info(
                                "event=advisory_sigma_gate_blocked trace=%s lane=resource "
                                "z_cpu=%s z_mem=%s threshold=%.1f — alert within normal bounds, no advisory",
                                trace,
                                _adv_z_cpu,
                                _adv_z_mem,
                                _adv_z_thr,
                            )
                            # Make the suppression VISIBLE on the pipeline instead of silently
                            # stopping at EVIDENCE: the 3σ baseline is ground truth, so a resource
                            # alert with z within bounds is a false positive and the advisory is
                            # correctly skipped. Operators see WHY the trace terminated.
                            _sigma_detail = (
                                f"3σ gate: z_cpu={_adv_z_cpu} z_mem={_adv_z_mem} "
                                f"within ±{_adv_z_thr:.1f}σ — advisory suppressed (false positive)"
                            )
                            await mark_stage(ctx.redis, trace, "RAG", "skip", detail="sigma_gate_suppressed")
                            await mark_stage(ctx.redis, trace, "LLM", "skip", detail=_sigma_detail)
                            await mark_stage(ctx.redis, trace, "SCHEMA", "skip", detail="no advisory (sigma gate)")
                            await mark_stage(ctx.redis, trace, "VERIFY", "skip", detail="no advisory to verify (sigma gate)")
                            await mark_stage(ctx.redis, trace, "KILLSWITCH", "skip", detail="no dispatch (sigma gate)")
                            await mark_stage(ctx.redis, trace, "CRAT", "skip", detail="no dispatch — nothing to audit (sigma gate)")
                            await mark_stage(ctx.redis, trace, "DISPATCH", "skip", detail="suppressed — no alert sent")
                            # Terminal stages (HITL/EXECUTOR/FEEDBACK) also do not apply on a
                            # suppressed path — resolve them so the trace shows 12/12, not "stuck".
                            await _mark_suggest_only_terminal(ctx, trace)
                            return ""
                    except Exception as _adv_snap_err:
                        logger.debug("advisory_sigma_gate snap parse error trace=%s err=%s", trace, _adv_snap_err)
            from workers.temporal_evidence_collector import fetch_temporal_evidence_for_batch
            from workers.advisory_analyst_handler import run_advisory_analyst

            try:
                # Fetch temporal evidence (1-hour historical metrics)
                temporal_block = await fetch_temporal_evidence_for_batch(ctx, batch, trace)
                sanitized_text = format_batch_sanitized_analyst_user_text(batch)
                if len(batch) == 1:
                    sanitized_text = format_sanitized_analyst_user_text(batch[0])

                if temporal_block:
                    sanitized_text = f"{sanitized_text}\n\n=== TEMPORAL EVIDENCE ===\n{temporal_block}"

                # Annotate 3-sigma baseline z-scores for the advisory analyst.
                # These come from baseline_snapshot stored in Redis and read by _proof_of_fault_gate.
                snap_raw = await ctx.redis.get(REDIS_KEY_SNAPSHOT)
                if snap_raw:
                    try:
                        _snap = json.loads(snap_raw.decode() if isinstance(snap_raw, bytes) else snap_raw)
                        _z_cpu = _f64(_snap.get("z_cpu"))
                        _z_mem = _f64(_snap.get("z_mem"))
                        _z_thr = float(getattr(ctx.settings, "baseline_dr_z_threshold", 3.0) or 3.0)
                        if _z_cpu is not None or _z_mem is not None:
                            _sigma_parts = []
                            if _z_cpu is not None:
                                _sigma_parts.append(f"z_cpu={_z_cpu:+.2f} ({'ANOMALY' if abs(_z_cpu) >= _z_thr else 'normal'})")
                            if _z_mem is not None:
                                _sigma_parts.append(f"z_mem={_z_mem:+.2f} ({'ANOMALY' if abs(_z_mem) >= _z_thr else 'normal'})")
                            sanitized_text = (
                                f"{sanitized_text}\n\n=== {SIGMA_RESOURCE_EVIDENCE_BASELINE_MARKER} ===\n"
                                f"threshold=±{_z_thr:.1f}σ | {' | '.join(_sigma_parts)}\n"
                                f"Anomaly = |z| ≥ {_z_thr:.1f}. Use this for forecast.basis when method=linear_extrapolation."
                            )
                            logger.info(
                                "event=sigma_baseline_injected trace=%s %s threshold=%.1f",
                                trace,
                                " ".join(_sigma_parts),
                                _z_thr,
                            )
                    except Exception:
                        pass

                # System Twin: cho bộ não thấy bản đồ hệ thống khách hàng đã verify
                # (dependency thật cho impact_chain thay vì đoán). Fail-open: twin
                # trống/lỗi → block rỗng, advisory tiếp tục bình thường.
                from workers.system_twin_context import build_system_twin_block

                _twin_block = await build_system_twin_block(
                    ctx.redis, _tenant_id_from_batch(batch)
                )
                if _twin_block:
                    sanitized_text = f"{sanitized_text}\n\n{_twin_block}"
                    logger.info(
                        "event=system_twin_injected trace=%s chars=%s", trace, len(_twin_block)
                    )

                # Customer-provided business docs (gap 4): ingest_customer_knowledge()
                # đã lưu metadata+summary nhưng chưa ai đọc lại trước LLM advisory.
                # Metadata/summary ONLY (INV_DATA_RESIDENCY) — chưa verify, đánh dấu rõ
                # nguồn để advisory không coi ngang hàng với bằng chứng probe thật.
                from workers.customer_knowledge_context import build_customer_knowledge_block

                _cust_kb_block = await build_customer_knowledge_block(
                    ctx.redis, _tenant_id_from_batch(batch)
                )
                if _cust_kb_block:
                    sanitized_text = f"{sanitized_text}\n\n{_cust_kb_block}"
                    logger.info(
                        "event=customer_knowledge_injected trace=%s chars=%s",
                        trace,
                        len(_cust_kb_block),
                    )

                # Pipeline stage: RAG — Redis Stack runs as Omni's SECOND BRAIN here.
                # Instead of a single one-shot recall, run a multi-turn RAG loop over the
                # vector store within ONE session for this alert (run_redis_brain). It
                # accumulates context across turns (each turn refines its query from what
                # it has learned), then injects that synthesized understanding into the LLM
                # prompt — so the LLM starts from Redis's view of the whole alert, not a
                # single snippet. A confident brain (strong hit) is flagged for the LLM.
                from rag.redis_brain import run_redis_brain

                _brain = await run_redis_brain(ctx, trace=trace, initial_query=sanitized_text)
                if _brain.accumulated_context:
                    _hdr = (
                        f"=== REDIS SECOND-BRAIN CONTEXT (multi-turn RAG · {_brain.turn_count} turns · "
                        f"top_score={_brain.top_score:.3f}"
                        + (" · CONFIDENT" if _brain.confident else "")
                        + ") ===\n"
                        "Prior verified knowledge for THIS alert, retrieved iteratively. "
                        "Treat high-score items as strong priors; reconcile with the live evidence below.\n"
                    )
                    sanitized_text = f"{_hdr}{_brain.accumulated_context}\n\n{sanitized_text}"
                    await mark_stage(
                        ctx.redis, trace, "RAG", "ok",
                        detail=f"second-brain turns={_brain.turn_count} top={_brain.top_score:.3f} confident={_brain.confident}",
                    )
                    # S2.4: persist point_id so the feedback loop can downvote on failure.
                    if _brain.answer_point_id:
                        try:
                            await ctx.redis.setex(
                                f"omni:recall:trace_point_id:{trace}", 7200, _brain.answer_point_id
                            )
                        except Exception:
                            pass
                else:
                    await mark_stage(
                        ctx.redis, trace, "RAG", "skip",
                        detail=f"second-brain no_hit turns={_brain.turn_count}",
                    )

                # Run advisory analyst (returns AnalystAdvisory schema)
                import time as _llm_time
                _llm_t0 = _llm_time.monotonic()
                await mark_stage(ctx.redis, trace, "LLM", "ok", detail="advisory_analyst/start")
                advisory = await run_advisory_analyst(
                    ctx,
                    payload={"chat_id": chat_id},
                    trace=trace,
                    evidence_text=sanitized_text,
                )
                _llm_elapsed_ms = int((_llm_time.monotonic() - _llm_t0) * 1000)
                await mark_stage(ctx.redis, trace, "LLM", "ok", detail=f"advisory_analyst elapsed_ms={_llm_elapsed_ms}")

                if advisory:
                    # Pipeline stage: advisory schema parsed/validated.
                    _adv_verdict = getattr(advisory, "verdict", "") or ""
                    await mark_stage(ctx.redis, trace, "SCHEMA", "ok", detail=f"verdict={_adv_verdict}")

                    # ── VERIFY stage — real diagnosis, not just doc lookup ──────────────
                    # Actually RUN the advisory's read-only verification_steps, then
                    # reconcile each recalled KB item against the live probe evidence and
                    # write the outcome back to the KB. HONEST GATE: a KB entry only ages
                    # (confirmed/refuted) when a probe ACTUALLY ran — no test ⇒ unverifiable.
                    try:
                        from workers.kb_verifier import run_readonly_verification
                        from rag.kb_feedback import apply_kb_feedback
                        from pkg.observability.pipeline_stages import append_trace_log

                        _probes = await run_readonly_verification(
                            ctx, advisory=advisory, trace=trace, max_probes=4
                        )
                        _ran = [p for p in _probes if getattr(p, "ran", False)]
                        for _p in _probes:
                            _lvl = "info" if _p.ran else ("warn" if _p.blocked else "info")
                            _st = "ran" if _p.ran else ("BLOCKED" if _p.blocked else "skip")
                            await append_trace_log(
                                ctx.redis, trace, "VERIFY",
                                f"{_st} rc={_p.rc} [{_p.layer}] {_p.command[:90]}"
                                + (f" — {_p.error[:60]}" if _p.error else ""),
                                level=_lvl,
                            )
                        _assess_raw = getattr(advisory, "kb_assessment", []) or []
                        _assessments = [
                            (a.model_dump() if hasattr(a, "model_dump") else dict(a))
                            for a in _assess_raw
                        ]
                        # GROUND-TRUTH RECONCILIATION — the LLM's kb_assessment is a
                        # self-graded hypothesis, NOT evidence. Read the claimed pod's
                        # live container status and let it judge the root_cause claim, so
                        # a hallucinated failure (e.g. "OOMKilled" on a healthy Running
                        # pod) is REFUTED instead of rubber-stamped confirmed. The
                        # reconciled verdict then CAPS the LLM's optimism.
                        from workers.verify_reconcile import cap_assessments, reconcile_advisory

                        # Stash the probe-evidence map so the multi-layer reconciler
                        # (OS/DB/host-metric/network) can read live ground truth without
                        # changing reconcile_advisory's stable signature. Read-only.
                        try:
                            ctx.evidence_by_probe = by_probe
                        except Exception:  # noqa: BLE001 — ctx may be slotted
                            pass
                        _recon = await reconcile_advisory(ctx, advisory)
                        await append_trace_log(
                            ctx.redis, trace, "VERIFY",
                            f"ground-truth={_recon.verdict} — {_recon.evidence[:160]}",
                            level=("warn" if _recon.verdict == "refuted" else "info"),
                        )
                        # No probe executed AND nothing read ⇒ we tested nothing ⇒ never
                        # score the KB on an untested guess.
                        if not _ran and _recon.verdict == "unverifiable":
                            for _a in _assessments:
                                _a["verdict"] = "unverifiable"
                        else:
                            _assessments = cap_assessments(_assessments, _recon.verdict)
                        _fb = await apply_kb_feedback(ctx.redis, trace=trace, assessments=_assessments)
                        await mark_stage(
                            ctx.redis, trace, "VERIFY", "ok",
                            detail=(
                                f"probes_ran={len(_ran)}/{len(_probes)} "
                                f"ground_truth={_recon.verdict} "
                                f"kb confirmed={_fb.get('confirmed', 0)} refuted={_fb.get('refuted', 0)} "
                                f"stale={len(_fb.get('stale_marked', []))}"
                            ),
                        )
                    except Exception as _verr:  # best-effort — never block dispatch
                        logger.warning("event=kb_verify_failed trace=%s err=%s", trace, _verr)
                        await mark_stage(ctx.redis, trace, "VERIFY", "skip", detail="verify error")
                    # ────────────────────────────────────────────────────────────────────

                    from workers.advisory_mode_kill_switch import AdvisoryModeKillSwitch
                    # Validate advisory output for forbidden mutation keywords (kill-switch layer 2)
                    _valid, _reason = AdvisoryModeKillSwitch.validate_advisor_output(
                        advisory.model_dump()
                    )
                    if not _valid:
                        logger.error(
                            "event=advisory_output_validation_failed trace=%s reason=%s",
                            trace,
                            _reason,
                        )
                    # Confirm execution gate is enforced and log it for the audit trail
                    _, _gate_reason = AdvisoryModeKillSwitch.validate_execution_gate(
                        tool_name="advisory_emit",
                        args={},
                        context="evidence_consumer_advisory",
                        auto_execute_enabled=bool(
                            getattr(ctx.settings, "omni_auto_execute_enabled", False)
                        ),
                        siem_suggest_only=bool(
                            getattr(ctx.settings, "omni_siem_suggest_only", True)
                        ),
                    )
                    logger.info(
                        "event=advisory_gate_confirmed trace=%s gate=%s", trace, _gate_reason
                    )
                    # Pipeline stage: killswitch validated.
                    _ks_status = "ok" if _valid else "fail"
                    await mark_stage(ctx.redis, trace, "KILLSWITCH", _ks_status, detail=_gate_reason or "")
                    # CRAT Fail-Closed Gate: audit write MUST succeed before any Telegram emit.
                    _audit_blk: dict[str, Any] = {}
                    try:
                        _audit_blk = await write_audit_block(
                            event_type="ADVISORY_DISPATCHED",
                            trace_id=trace,
                            payload=advisory.model_dump(),
                            redis=ctx.redis,
                            kafka=ctx.kafka,
                            kafka_topic=ctx.settings.kafka_topic_audit_chain,
                        )
                    except AuditLedgerError as _audit_err:
                        logger.critical(
                            "event=audit_chain_write_failed phase=evidence_consumer trace=%s err=%s FAIL_CLOSED",
                            trace,
                            _audit_err,
                        )
                        await mark_stage(ctx.redis, trace, "CRAT", "fail", detail="audit_chain_write_failed")
                        return "[ADVISORY MODE FAIL_CLOSED] audit_chain_write_failed — dispatch aborted"
                    await mark_stage(ctx.redis, trace, "CRAT", "ok", detail="ADVISORY_DISPATCHED")
                    # Emit advisory to Telegram — prefer request chat_id, fall back to admin chat_id
                    effective_cid = chat_id or getattr(ctx.settings, "telegram_admin_chat_id", None)
                    if effective_cid is not None:
                        # Telegram-only copy: CRAT above used original ``advisory`` unchanged.
                        tg_advisory = copy_advisory_for_telegram_if_mismatch(
                            advisory, sanitized_text
                        )
                        # Resolve lane for badge — best-effort, no extra Redis call.
                        try:
                            _tg_lane, _ = resolve_proof_lane(batch)
                        except Exception:
                            _tg_lane = ""
                        _adv_audit = AuditMeta(
                            mode=getattr(ctx.settings, "omni_autonomy_tier", None),
                            decision="SUGGEST",
                            action="Chỉ đề xuất — analyst chỉ đọc, không tự thực thi",
                            crat_seq=_audit_blk.get("seq") if _audit_blk else None,
                            crat_signed=bool(_audit_blk.get("signature_hex")) if _audit_blk else False,
                            crat_event="ADVISORY_DISPATCHED",
                        )
                        await render_advisory_to_telegram(
                            ctx, tg_advisory, int(effective_cid), lane_label=_tg_lane, audit=_adv_audit
                        )
                    elif ctx.telegram:
                        logger.error(
                            "event=advisory_no_chat_id trace=%s — set OMNI_TELEGRAM_ADMIN_CHAT_ID to receive autonomous advisories",
                            trace,
                        )
                    # Log escalation tier for observability and CRAT record.
                    _tier = getattr(advisory, "escalation_tier", "L2_SUGGEST")
                    logger.info(
                        "event=advisory_escalation_tier trace=%s tier=%s confidence=%s verdict=%s",
                        trace, _tier, advisory.confidence, advisory.verdict,
                    )

                    # Phase 7.3 — HITL routing: tier=L3_HITL or escalation_reason (default gate off)
                    if (
                        (_tier == "L3_HITL" or advisory.escalation_reason)
                        and getattr(ctx.settings, "omni_hitl_routing_enabled", False)
                    ):
                        from workers.advisory_hitl_compat import AdvisoryHITLCompat
                        hitl_ok, _ = AdvisoryHITLCompat.validate_hitl_gate(
                            trace, context="evidence_consumer_advisory", settings=ctx.settings
                        )
                        if hitl_ok:
                            # CRAT Fail-Closed: audit write MUST succeed before Kafka send
                            try:
                                await write_audit_block(
                                    event_type="HITL_ESCALATION_EMITTED",
                                    trace_id=trace,
                                    payload={
                                        "escalation_reason": advisory.escalation_reason,
                                        "verdict": advisory.verdict,
                                        "tool_name": "human_escalation",
                                    },
                                    redis=ctx.redis,
                                    kafka=ctx.kafka,
                                    kafka_topic=ctx.settings.kafka_topic_audit_chain,
                                )
                            except AuditLedgerError as _hitl_audit_err:
                                logger.critical(
                                    "event=hitl_audit_write_failed trace=%s err=%s FAIL_CLOSED",
                                    trace,
                                    _hitl_audit_err,
                                )
                            else:
                                await emit_hitl_pending(
                                    ctx,
                                    trace=trace,
                                    tool_name="human_escalation",
                                    args={"escalation_reason": advisory.escalation_reason},
                                    hitl_reason=advisory.escalation_reason,
                                )
                                logger.info(
                                    "event=hitl_escalation_emitted trace=%s reason=%s",
                                    trace,
                                    advisory.escalation_reason[:200],
                                )
                    # Emit as SUGGEST_REMEDIATION (not mutations); tier is metadata only at this point.
                    _tier = getattr(advisory, "escalation_tier", "L2_SUGGEST")
                    from pkg.reasoning.analyst_advisory_schema import confidence_to_float

                    await _emit_suggest_remediation(
                        ctx,
                        trace=trace,
                        diagnosis=advisory.root_cause,
                        # Honest projection of the advisory confidence literal — a medium/low
                        # LLM verdict must not reach the operator inflated to 0.9.
                        confidence=confidence_to_float(advisory.confidence),
                        source=f"ADVISORY_MODE_ANALYST/{_tier}",
                        suggested_tool="kubectl_describe",
                        audit=False,  # CRAT (ADVISORY_DISPATCHED) written above at the killswitch gate
                    )
                    logger.info(
                        "event=advisory_analyst_complete trace=%s verdict=%s chat_id=%s",
                        trace,
                        advisory.verdict,
                        effective_cid,
                    )
                    await emit_transition(
                        ctx,
                        trace_id=trace,
                        transition=TRANSITION_PLAN_EMITTED,
                        component="evidence_consumer",
                        detail="advisory_analyst_generated",
                    )
                    await _mark_suggest_only_terminal(ctx, trace)
                    return f"[ADVISORY MODE] {advisory.verdict}: {advisory.root_cause}"
                else:
                    # LLM returned None (parse failure) — FAIL-CLOSED, never fall through to planner.
                    logger.warning(
                        "event=advisory_analyst_null trace=%s — no advisory generated, failing closed",
                        trace,
                    )
                    return "[ADVISORY MODE DEGRADED] advisory=None"
            except Exception as e:
                logger.warning(
                    "event=advisory_analyst_error trace=%s err=%s",
                    trace,
                    str(e)[:200],
                    exc_info=True,
                )
                # FAIL-CLOSED: never fall through to the traditional planner.
                # The planner can emit EXECUTE_MUTATE; in advisory mode that is an invariant violation.
                # Emit a degraded Telegram alert so operators are notified.
                effective_cid = chat_id or getattr(ctx.settings, "telegram_admin_chat_id", None)
                if effective_cid and getattr(ctx, "telegram", None):
                    try:
                        await ctx.telegram.send_message(
                            int(effective_cid),
                            (
                                "⚠️ *Advisory Analyst Degraded*\n"
                                f"Trace: `{trace}`\n"
                                "The advisory analyst encountered an error and produced no analysis. "
                                "*No automated action has been taken.* "
                                "Please investigate manually.\n"
                                f"Error: `{str(e)[:300]}`"
                            ),
                            parse_mode="Markdown",
                        )
                    except Exception:
                        logger.error("event=advisory_degraded_telegram_error trace=%s", trace)
                return f"[ADVISORY MODE DEGRADED] trace={trace} err={str(e)[:100]}"

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
                # F22-residual: the SDK-only LLM returned ESCALATE because it could not name a
                # tool. For a KNOWN rollout-eligible workload fault (NotReady/crashloop/OOM/CPU)
                # with a resolvable target (alert labels OR owner_reference), synthesize the
                # rollout deterministically and run it through every mutate gate — do NOT depend
                # on the flaky agentic planner converging. Hoisted ABOVE partial-identity-suggest
                # so the full alert→mutate chain closes without an LLM tool name.
                _forced = await _synthesize_rollout_safety_net_plan(ctx, batch)
                if _forced is not None:
                    logger.info(
                        "event=sdk_miss_hoisted_safety_net trace=%s tool=%s",
                        trace, str(_forced.get("tool_name") or ""),
                    )
                    _hoisted = await _emit_agentic_mutate_if_any(
                        ctx, trace, batch,
                        sanitized_text=sanitized_text,
                        playbook=matched_playbook,
                        forced_plan=_forced,
                    )
                    if _hoisted:
                        return display_out
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

                _tg_ns = _batch_identity.get("namespace") or ""
                _tg_pod = _batch_identity.get("pod") or ""
                _tg_dep = _batch_identity.get("deployment") or ""
                _tg_alertname = alertname_from_batch(batch) or "UnknownAlert"
                _tg_severity = ""
                if batch:
                    _tg_severity = str(batch[0].get("severity") or "").strip()
                # Problem
                _resource = _tg_dep or _tg_pod or "?"
                _ns_disp = _tg_ns or "?"
                _sev_suf = f" [{_tg_severity}]" if _tg_severity else ""
                _problem = f"{_tg_alertname} on {_ns_disp}/{_resource}{_sev_suf}"
                # Reason
                _gaps: list[str] = []
                if not _tg_ns:
                    _gaps.append("namespace")
                if not _tg_pod:
                    _gaps.append("pod")
                if not _tg_dep:
                    _gaps.append("deployment")
                if human:
                    _reason = human[:400].strip()
                elif _gaps:
                    _reason = "identity incomplete — missing " + ", ".join(_gaps) + "; RAG returned no matching runbook"
                else:
                    _reason = "RAG miss — no runbook matched; LLM produced no hypothesis"
                # Chain: correlated events in this batch (state → app_log → metrics)
                _chain: list[str] = []
                for _ev in (batch or [])[:6]:
                    _ar = str(_ev.get("alert_rule") or "").strip()[:80]
                    _ah = str(_ev.get("alert_hint") or "").strip()[:100]
                    _ln = str(_ev.get("lane") or _ev.get("source") or "").strip()
                    _ts = str(_ev.get("timestamp") or _ev.get("fired_at") or "").strip()[:19]
                    parts: list[str] = []
                    if _ts:
                        parts.append(_ts)
                    if _ln:
                        parts.append(f"[{_ln}]")
                    if _ar:
                        parts.append(_ar)
                    if _ah:
                        parts.append(f"— {_ah}")
                    if parts:
                        _chain.append(" ".join(parts))
                # Advise
                _advise: list[str] = []
                if _tg_ns and _tg_dep:
                    _advise.append(f"kubectl describe deployment {_tg_dep} -n {_tg_ns}")
                    _advise.append(f"kubectl get pods -n {_tg_ns} -l app={_tg_dep} --show-labels")
                    _advise.append(f"kubectl logs deployment/{_tg_dep} -n {_tg_ns} --tail=100")
                elif _tg_ns and _tg_pod:
                    _advise.append(f"kubectl describe pod {_tg_pod} -n {_tg_ns}")
                    _advise.append(f"kubectl logs {_tg_pod} -n {_tg_ns} --tail=200")
                elif _tg_ns:
                    _advise.append(f"kubectl get pods -n {_tg_ns} --show-labels --sort-by=.status.startTime")
                    _advise.append(f"kubectl get events -n {_tg_ns} --sort-by=.lastTimestamp | tail -30")
                else:
                    _advise.append("identity missing — confirm source SIEM envelope has namespace/pod labels")
                _advise.append("if no runbook: add to RAG collection `sop_runbooks` so next occurrence auto-remediates")
                _tg_card = format_operator_triage_card(
                    problem=_problem,
                    reason=_reason,
                    chain=_chain,
                    advise=_advise,
                ) + "\n\n" + render_audit_footer(
                    str(trace),
                    AuditMeta(
                        mode=getattr(ctx.settings, "omni_autonomy_tier", None),
                        decision="HITL",
                        origin="escalation",
                        action="Chuyển on-call — RAG + LLM không đưa được giả thuyết",
                        crat_event="ADVISORY_DISPATCHED",
                    ),
                    markdown=False,
                )
                _SYNTHETIC_ALERTNAMES = {"FullAudit", "ChaosLabAlert", "SIEMUnknown", "GenericAlert"}
                # SIEM batches originate from external FinGuard system — never suppress, even if
                # the prober fell back to GenericAlert alertname due to missing workload context.
                _is_siem = _is_siem_batch(batch or [])
                if _tg_alertname in _SYNTHETIC_ALERTNAMES and not _is_siem:
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
