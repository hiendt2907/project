from __future__ import annotations

import json
import logging
import time
from typing import Any

from workers.diagnostic_evidence import evidence_from_probe
from workers.diagnostic_mapping import alertname_from_anomaly_event, classify_event, load_diagnostic_matrix
from workers.diagnostic_pod_plan import get_smart_diagnostic_plan, snapshot_from_structured_hint
from workers.diagnostic_probe_registry import run_probe
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
    payload = {
        "kind": "diagnostic_evidence",
        "trace_id": trace,
        "symptom_group": symptom_group,
        "layer": layer,
        "probe": ev_obj.probe_name,
        "result": ev_obj.result,
        "extracted_fact": ev_obj.extracted_fact,
        "raw": redact(ev_obj.raw_output)[:4000],
        "ts": str(int(time.time())),
        "alert_rule": getattr(ev, "rule_name", "") or "",
        "alert_hint": redact(ev.error_hint or "")[:800],
        "canonical_query_snippet": redact(ev.canonical_query or "")[:1000],
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


async def run_diagnostic_pipeline(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> None:
    """Deterministic probes from YAML matrix or smart tier-1/tier-2 plan → Kafka diagnostic evidence."""
    ws = ctx.settings
    if not ws.diagnostic_dictionary_enabled:
        return
    matrix = load_diagnostic_matrix(ws.diagnostic_matrix_path)
    trace = ev.trace_id

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
        logger.debug("diagnostic: no matrix row for trace=%s", ev.trace_id)
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
