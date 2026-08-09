from __future__ import annotations

import json
import logging
import time
from typing import Any

from workers.diagnostic_evidence import evidence_from_probe
from workers.diagnostic_mapping import alertname_from_anomaly_event, classify_event, load_diagnostic_matrix
from workers.diagnostic_pod_plan import get_smart_diagnostic_plan, snapshot_from_structured_hint
from workers.diagnostic_probe_registry import PROBE_DOMAINS, run_probe
from workers.diagnostic_resource import (
    is_kube_pod_container_state_alert,
    is_workload_resource_alert,
    pod_identity_from_event,
)
from workers.evidence_batch import register_diag_expected_probes
from workers.handlers import WorkerHandlerContext
from workers.log_preview import json_obj_preview
from observability.normalize import redact
from workers.proactive_models import AnomalyEvent

logger = logging.getLogger(__name__)

_LEGACY_TIER2: list[str] = [
    "k8s_clinical_pod_metrics",
    "k8s_clinical_pod_log_tail",
    "prom_pod_cpu_cores",
    "prom_pod_memory_wss",
]

# Prometheus alertname → security probes (bypass generic matrix catch-all).
_ALERTNAME_PROBE_MAP: dict[str, list[str]] = {
    "OmniRbacClusterAdminViolation": ["rbac_drift"],  # RBAC drift / cluster-admin binding
    "OmniConfigMapGodModeProd": ["configmap_security_drift"],  # dangerous ConfigMap keys
}


def probe_ids_for_alertname(alertname: str) -> list[str]:
    """Probes used for alertname (security self-remediation) — same as dispatcher plan."""
    an = (alertname or "").strip()
    if not an:
        return []
    return list(_ALERTNAME_PROBE_MAP.get(an, []))


def _evidence_source_for_probe(probe_id: str) -> str:
    if probe_id.startswith("k8s_clinical_") or probe_id.startswith("k8s_events") or probe_id.startswith(
        "k8s_resource"
    ):
        return "K8s_SDK"
    if probe_id.startswith("prom_"):
        return "Prometheus"
    return "other"


async def _publish_diagnostic_evidence(
    ctx: WorkerHandlerContext,
    ev: AnomalyEvent,
    *,
    trace: str,
    pid: str,
    symptom_group: str,
    layer: str,
    raw: Any,
) -> None:
    ws = ctx.settings
    ev_obj = evidence_from_probe(raw, trace)
    _ev_ns, _ev_pod, _ = pod_identity_from_event(ev)
    _ev_dep = str(getattr(ev, "deployment", "") or "").strip()
    payload = {
        "kind": "diagnostic_evidence",
        "trace_id": trace,
        "symptom_group": symptom_group,
        "layer": layer,
        "probe": ev_obj.probe_name,
        # Lĩnh vực canonical theo REGISTRY của chính probe — nguồn tự khai, không suy
        # đoán. `evidence_consumer` đọc `ev_doc["domain"]` ở mark_stage EVIDENCE;
        # envelope của đường alert trước đây không có khoá này nên 100% trace sống
        # hiện `domain=""` (đo tại P1, mục #11). PROBE_DOMAINS có bất biến kiểm mọi
        # probe đều được phân loại (diagnostic_probe_registry.py:512-523).
        "domain": PROBE_DOMAINS.get(pid, ""),
        "signal_kind": "diagnostic",
        "result": ev_obj.result,
        "extracted_fact": ev_obj.extracted_fact,
        "raw": redact(ev_obj.raw_output)[:4000],
        "ts": str(int(time.time())),
        "alert_rule": getattr(ev, "rule_name", "") or "",
        "alert_hint": redact(ev.error_hint or "")[:800],
        "canonical_query_snippet": redact(ev.canonical_query or "")[:1000],
        "namespace": _ev_ns,
        "pod": _ev_pod,
        "deployment": _ev_dep,
        "evidence_source": _evidence_source_for_probe(pid),
        "clinical_priority_note": (
            "Primary: real-time Kubernetes API (SDK)."
            if _evidence_source_for_probe(pid) == "K8s_SDK"
            else (
                "Secondary: historical Prometheus (may lag vs live cluster)."
                if _evidence_source_for_probe(pid) == "Prometheus"
                else ""
            )
        ),
    }
    assert ctx.kafka is not None
    await ctx.kafka.send_dict(ws.kafka_topic_diagnostic_evidence, {"data": json.dumps(payload, ensure_ascii=False)})
    logger.info(
        "[%s] event=diagnostic_evidence_publish topic=%s probe=%s kafka_payload_preview=%s",
        trace,
        ws.kafka_topic_diagnostic_evidence,
        ev_obj.probe_name,
        json_obj_preview(payload, max_chars=1400),
    )


async def _publish_siem_synthetic_evidence(
    ctx: WorkerHandlerContext,
    ev: AnomalyEvent,
    *,
    trace: str,
) -> None:
    """Synthesize a SIEM incident evidence document from the AnomalyEvent (no K8s probes)."""
    incident_facts: dict[str, Any] = {}
    try:
        cq = json.loads(ev.canonical_query)
        labels = cq.get("labels") or {}
        annot = cq.get("annotations") or {}
        incident_facts = {
            k: v for k, v in {
                "category": labels.get("siem_category", ""),
                "severity": labels.get("severity", ""),
                "incident_id": labels.get("siem_incident_id", ""),
                "tenant": labels.get("siem_tenant", ""),
                "description": annot.get("description", ""),
                "suggested_action": annot.get("suggested_action", ""),
                "affected_ip": annot.get("affected_ip", ""),
                "namespace": labels.get("namespace", ""),
            }.items() if v
        }
    except Exception:
        pass

    payload = {
        "kind": "diagnostic_evidence",
        "trace_id": trace,
        "symptom_group": "siem_incident",
        "layer": "security",
        "probe": "siem_incident_context",
        "domain": "security",
        "signal_kind": "diagnostic",
        "result": "SIEM_INCIDENT",
        "extracted_fact": incident_facts,
        "raw": redact(ev.error_hint)[:4000],
        "ts": str(int(time.time())),
        "alert_rule": ev.rule_name,
        "alert_hint": redact(ev.error_hint)[:800],
        "canonical_query_snippet": redact(ev.canonical_query)[:1000],
        "namespace": incident_facts.get("namespace", ev.namespace),
        "pod": "",
        "deployment": "",
        "evidence_source": "SIEM",
        "clinical_priority_note": "Primary: FinGuard/Smart-SIEM real-time security incident.",
    }
    assert ctx.kafka is not None
    await ctx.kafka.send_dict(
        ctx.settings.kafka_topic_diagnostic_evidence,
        {"data": json.dumps(payload, ensure_ascii=False)},
    )
    logger.info(
        "[%s] event=siem_synthetic_evidence_published alertname=%s category=%s",
        trace,
        ev.rule_name,
        incident_facts.get("category", ""),
    )


async def _publish_syshardtail_synthetic_evidence(
    ctx: WorkerHandlerContext,
    ev: AnomalyEvent,
    *,
    trace: str,
) -> None:
    """Synthesize evidence for SysHardFail* OS/DB/network alerts (no K8s probes available)."""
    facts: dict[str, Any] = {}
    try:
        cq = json.loads(ev.canonical_query)
        labels = cq.get("labels") or {}
        annot = cq.get("annotations") or {}
        facts = {
            k: v for k, v in {
                "alertname": labels.get("alertname", ""),
                "host": labels.get("host", "") or labels.get("instance", ""),
                "severity": labels.get("severity", ""),
                "job": labels.get("job", ""),
                "summary": annot.get("summary", ""),
                "description": annot.get("description", ""),
            }.items() if v
        }
    except Exception:
        pass

    payload = {
        "kind": "diagnostic_evidence",
        "trace_id": trace,
        "symptom_group": "infra_hard_fail",
        "layer": "os_baremetal",
        "probe": "alert_context",
        # Ngữ cảnh alert thô chưa gắn probe nào ⇒ chưa biết lĩnh vực. Để RỖNG chứ
        # không đoán: rỗng còn được call site sau lấp (last-non-empty-wins), còn một
        # giá trị sai thì đứng nguyên và hiện lên portal như lĩnh vực có thật.
        "domain": "",
        "signal_kind": "diagnostic",
        "result": "FAILED",
        "extracted_fact": facts,
        "raw": redact(ev.error_hint or "")[:4000],
        "ts": str(int(time.time())),
        "alert_rule": ev.rule_name,
        "alert_hint": redact(ev.error_hint or "")[:800],
        "canonical_query_snippet": redact(ev.canonical_query or "")[:1000],
        "namespace": ev.namespace or "",
        "pod": "",
        "deployment": "",
        "evidence_source": "other",
        "clinical_priority_note": "Primary: Prometheus alert from OS/DB/network monitoring.",
    }
    assert ctx.kafka is not None
    await ctx.kafka.send_dict(
        ctx.settings.kafka_topic_diagnostic_evidence,
        {"data": json.dumps(payload, ensure_ascii=False)},
    )
    logger.info(
        "[%s] event=syshardtail_synthetic_evidence_published alertname=%s host=%s",
        trace,
        facts.get("alertname") or ev.rule_name,
        facts.get("host", ""),
    )


async def run_diagnostic_pipeline(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> None:
    """Deterministic probes from YAML matrix or smart tier-1/tier-2 plan → Kafka diagnostic evidence."""
    ws = ctx.settings
    if not ws.diagnostic_dictionary_enabled:
        return
    matrix = load_diagnostic_matrix(ws.diagnostic_matrix_path)
    trace = ev.trace_id

    # SIEM incidents: no K8s probes; synthesize evidence from AnomalyEvent directly.
    # Detect SIEM via rule_name (siem-bridge path) OR alert labels (Prometheus webhook path).
    _is_siem = ev.rule_name.startswith("SIEM")
    if not _is_siem:
        try:
            _cq = json.loads(ev.canonical_query or "{}")
            _lbl = _cq.get("labels") if isinstance(_cq, dict) else None
            if isinstance(_lbl, dict) and (_lbl.get("siem_source") or _lbl.get("siem_category")):
                _is_siem = True
        except Exception:
            pass
    if _is_siem:
        await register_diag_expected_probes(ctx.redis, trace, ["siem_incident_context"])
        await _publish_siem_synthetic_evidence(ctx, ev, trace=trace)
        return

    if is_workload_resource_alert(ev) or is_kube_pod_container_state_alert(ev):
        symptom_group = "workload_resource" if is_workload_resource_alert(ev) else "pod_container_state"
        layer = "workload"
        stop_on_first_failure = False
        ns, pod, _ = pod_identity_from_event(ev)
        mode = "workload_resource" if symptom_group == "workload_resource" else "pod_state"

        status_raw = await run_probe("k8s_clinical_pod_status", ctx, ev)
        hist = status_raw.structured_hint if isinstance(status_raw.structured_hint, dict) else {}
        if status_raw.status == "PASSED" and hist.get("kind") == "PodStatus":
            snap = snapshot_from_structured_hint(hist)
            plan = get_smart_diagnostic_plan(snap, mode=mode)
        else:
            plan = list(_LEGACY_TIER2)
            logger.warning(
                "[%s] event=diagnostic_smart_fallback reason=status_not_passed status=%s",
                trace,
                status_raw.status,
            )

        expected = ["k8s_clinical_pod_status"] + plan
        await register_diag_expected_probes(ctx.redis, trace, expected)

        logger.info(
            "[%s] event=diagnostic_dispatcher_plan kind=smart_tier2 ns=%s pod=%s mode=%s plan=%s",
            trace,
            ns,
            pod,
            mode,
            plan,
        )

        await _publish_diagnostic_evidence(
            ctx,
            ev,
            trace=trace,
            pid="k8s_clinical_pod_status",
            symptom_group=symptom_group,
            layer=layer,
            raw=status_raw,
        )

        for pid in plan:
            raw = await run_probe(pid, ctx, ev)
            await _publish_diagnostic_evidence(
                ctx,
                ev,
                trace=trace,
                pid=pid,
                symptom_group=symptom_group,
                layer=layer,
                raw=raw,
            )
            if stop_on_first_failure and raw.status == "FAILED":
                break
        return

    alertname = alertname_from_anomaly_event(ev)
    if alertname in _ALERTNAME_PROBE_MAP:
        probe_ids = _ALERTNAME_PROBE_MAP[alertname]
        symptom_group = "security_hardening"
        layer = "security"
        stop_on_first_failure = False
        await register_diag_expected_probes(ctx.redis, trace, list(probe_ids))
        logger.info(
            "[%s] event=diagnostic_dispatcher_plan kind=alertname_probe_map alertname=%s plan=%s",
            trace,
            alertname,
            probe_ids,
        )
        for pid in probe_ids:
            raw = await run_probe(pid, ctx, ev)
            await _publish_diagnostic_evidence(
                ctx,
                ev,
                trace=trace,
                pid=pid,
                symptom_group=symptom_group,
                layer=layer,
                raw=raw,
            )
            if stop_on_first_failure and raw.status == "FAILED":
                break
        return

    row = classify_event(ev, matrix)
    if not row:
        alertname = alertname_from_anomaly_event(ev)
        an_lower = alertname.lower()
        if an_lower.startswith("syshardfail") or an_lower.startswith("chaosdrillbare") or an_lower.startswith("chaosdrillsiem"):
            await register_diag_expected_probes(ctx.redis, trace, ["alert_context"])
            await _publish_syshardtail_synthetic_evidence(ctx, ev, trace=trace)
        else:
            logger.debug("diagnostic: no matrix row for trace=%s alertname=%s", ev.trace_id, alertname)
        return
    probe_ids = row.probe_ids
    symptom_group = row.symptom_group
    layer = row.layer
    stop_on_first_failure = row.stop_on_first_failure

    await register_diag_expected_probes(ctx.redis, trace, list(probe_ids))

    for pid in probe_ids:
        raw = await run_probe(pid, ctx, ev)
        await _publish_diagnostic_evidence(
            ctx,
            ev,
            trace=trace,
            pid=pid,
            symptom_group=symptom_group,
            layer=layer,
            raw=raw,
        )
        if stop_on_first_failure and raw.status == "FAILED":
            break
