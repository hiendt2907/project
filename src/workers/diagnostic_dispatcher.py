from __future__ import annotations

import json
import logging
import time

from workers.diagnostic_evidence import evidence_from_probe
from workers.diagnostic_mapping import classify_event, load_diagnostic_matrix
from workers.diagnostic_probe_registry import run_probe
from workers.diagnostic_resource import is_workload_resource_alert, pod_identity_from_event, resource_probe_ids
from workers.handlers import WorkerHandlerContext
from workers.log_preview import json_obj_preview
from observability.normalize import redact
from workers.proactive_models import AnomalyEvent

logger = logging.getLogger(__name__)


def _evidence_source_for_probe(probe_id: str) -> str:
    if probe_id.startswith("k8s_clinical_"):
        return "K8s_SDK"
    if probe_id.startswith("prom_"):
        return "Prometheus"
    return "other"


async def run_diagnostic_pipeline(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> None:
    """Deterministic probes from YAML matrix → Kafka topic ``kafka_topic_diagnostic_evidence``."""
    ws = ctx.settings
    if not ws.diagnostic_dictionary_enabled:
        return
    matrix = load_diagnostic_matrix(ws.diagnostic_matrix_path)
    trace = ev.trace_id

    if is_workload_resource_alert(ev):
        probe_ids = resource_probe_ids()
        symptom_group = "workload_resource"
        layer = "workload"
        stop_on_first_failure = False
        ns, pod, _ = pod_identity_from_event(ev)
        logger.info(
            "[%s] event=diagnostic_dispatcher_plan kind=resource ns=%s pod=%s probes=%s",
            trace,
            ns,
            pod,
            probe_ids,
        )
    else:
        row = classify_event(ev, matrix)
        if not row:
            logger.debug("diagnostic: no matrix row for trace=%s", ev.trace_id)
            return
        probe_ids = row.probe_ids
        symptom_group = row.symptom_group
        layer = row.layer
        stop_on_first_failure = row.stop_on_first_failure

    for pid in probe_ids:
        raw = await run_probe(pid, ctx, ev)
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
            "canonical_query_snippet": redact(ev.canonical_query or "")[:500],
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
        if stop_on_first_failure and raw.status == "FAILED":
            break
