"""Map inbound alert payloads (gateway / telegram) to AnomalyEvent for diagnostic probes."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from pkg.autonomy.gigo import build_gigo_metadata
from pkg.reasoning.alert_identity import SignalDNA, parse_signal_dna_from_labels
from workers.proactive_models import AnomalyEvent

logger = logging.getLogger(__name__)

def _stringify_labels(raw: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if v is None:
            continue
        key = str(k).strip()
        if not key:
            continue
        if isinstance(v, (dict, list)):
            continue
        out[key] = str(v).strip()
    return out


def _prometheus_canonical_document(labels: dict[str, str], annot: dict[str, str]) -> str:
    """JSON for ``canonical_query``: ``labels`` before ``annotations`` so evidence ``[:500]`` truncation
    still includes routing keys (namespace, deployment) for deterministic OOM / matrix matching.
    Inner ``labels`` / ``annotations`` dicts keep sorted keys for stability."""
    doc = {"labels": dict(sorted(labels.items())), "annotations": dict(sorted(annot.items()))}
    raw = json.dumps(doc, ensure_ascii=False)
    return raw[:2000]


def anomaly_event_dict_from_evidence_batch(
    batch: list[dict[str, Any]],
    trace: str,
) -> dict[str, Any]:
    """
    Reconstruct a dict suitable for ``AnomalyEvent.model_validate`` from diagnostic evidence batch
    (same trace as ingress) — used for post-mutate SDK verify probes in analyst.
    """
    tid = str(trace or "").strip() or "evidence-unknown"
    if not batch:
        return {
            "trace_id": tid,
            "rule_name": "IngressPrometheus",
            "target": "cluster",
            "namespace": "",
            "metric_value": 0.0,
            "threshold": 0.0,
            "canonical_query": "{}",
            "timestamp": "",
            "trigger_promql": "",
            "error_hint": "",
            "gigo_metadata": {},
        }
    b0 = batch[0]
    cq = str(b0.get("canonical_query_snippet") or "").strip()
    if not cq:
        cq = "{}"
    ar = str(b0.get("alert_rule") or "IngressPrometheus").strip() or "IngressPrometheus"
    ns = ""
    dna = SignalDNA()
    if cq.startswith("{"):
        try:
            j = json.loads(cq)
            labels = j.get("labels") if isinstance(j, dict) else None
            if isinstance(labels, dict):
                sl = {
                    str(k): str(v).strip()
                    for k, v in labels.items()
                    if v is not None and not isinstance(v, (dict, list))
                }
                dna = parse_signal_dna_from_labels(sl)
                ns = dna.namespace
        except Exception:
            pass
    ah = str(b0.get("alert_hint") or "")[:800]
    gigo: dict[str, str] = {}
    if cq.startswith("{"):
        try:
            j = json.loads(cq)
            if isinstance(j, dict):
                raw_l = j.get("labels")
                raw_a = j.get("annotations")
                if isinstance(raw_l, dict):
                    sl = {
                        str(k): str(v).strip()
                        for k, v in raw_l.items()
                        if v is not None and not isinstance(v, (dict, list))
                    }
                    an_d: dict[str, str] = {}
                    if isinstance(raw_a, dict):
                        an_d = {
                            str(k): str(v).strip()
                            for k, v in raw_a.items()
                            if v is not None and not isinstance(v, (dict, list))
                        }
                    gigo = build_gigo_metadata(sl, an_d)
        except Exception:
            pass
    out: dict[str, Any] = {
        "trace_id": tid,
        "rule_name": ar[:256],
        "target": "cluster",
        "namespace": ns,
        "metric_value": 0.0,
        "threshold": 0.0,
        "canonical_query": cq[:2000],
        "timestamp": str(b0.get("ts") or ""),
        "trigger_promql": "",
        "error_hint": ah,
        "symptom_group": dna.symptom_group,
        "drift_type": dna.drift_type,
        "deployment": dna.deployment,
        "omni_layer": dna.omni_layer,
        "omni_verify_required": dna.omni_verify_required,
        "gigo_metadata": gigo,
    }
    return out


def build_anomaly_event_from_alert_payload(payload: dict[str, Any]) -> AnomalyEvent:
    """Build a minimal AnomalyEvent so the diagnostic matrix can classify and run probes."""
    trace_id = str(payload.get("trace_id") or f"alert-{uuid.uuid4().hex[:12]}")
    source = str(payload.get("source") or "unknown")

    if source == "prometheus":
        body = payload.get("data")
        if not isinstance(body, dict):
            body = {}
        alerts = body.get("alerts") or []
        a0: dict[str, Any] = alerts[0] if alerts and isinstance(alerts[0], dict) else {}
        labels = _stringify_labels(a0.get("labels") if isinstance(a0.get("labels"), dict) else None)
        annot = _stringify_labels(a0.get("annotations") if isinstance(a0.get("annotations"), dict) else None)
        dna: SignalDNA = parse_signal_dna_from_labels(labels)
        alertname = dna.alertname or "unknown_alert"
        ns = dna.namespace
        desc = str(annot.get("description") or annot.get("summary") or "")
        identity_bits = list(dna.identity_bits_for_error_hint())
        for key in ("pod", "pod_name", "container", "statefulset", "daemonset"):
            if labels.get(key):
                identity_bits.append(f"{key}={labels[key]}")
        eh_core = f"{alertname} {' '.join(identity_bits)} {desc}".strip()
        eh = eh_core[:800] if eh_core else f"{alertname} {desc}"[:800]
        cq = _prometheus_canonical_document(labels, annot)
        trigger = str(annot.get("query") or annot.get("promql") or payload.get("trigger_promql") or "")[:2000]
        gigo = build_gigo_metadata(labels, annot)
        return AnomalyEvent(
            trace_id=trace_id,
            rule_name="IngressPrometheus",
            target="cluster",
            namespace=ns,
            metric_value=0.0,
            threshold=0.0,
            canonical_query=cq,
            timestamp=str(int(payload.get("received_at") or 0)),
            trigger_promql=trigger,
            error_hint=eh[:800],
            symptom_group=dna.symptom_group,
            drift_type=dna.drift_type,
            deployment=dna.deployment,
            omni_layer=dna.omni_layer,
            omni_verify_required=dna.omni_verify_required,
            gigo_metadata=gigo,
        )

    if source == "siem":
        body = payload.get("data")
        if not isinstance(body, dict):
            body = {}
        alerts = body.get("alerts") or []
        a0: dict[str, Any] = alerts[0] if alerts and isinstance(alerts[0], dict) else {}
        labels = _stringify_labels(a0.get("labels") if isinstance(a0.get("labels"), dict) else None)
        annot = _stringify_labels(a0.get("annotations") if isinstance(a0.get("annotations"), dict) else None)
        alertname = labels.get("alertname") or "SIEMUnknown"
        ns = labels.get("namespace") or ""
        severity = labels.get("severity") or ""
        category = labels.get("siem_category") or ""
        incident_id = labels.get("siem_incident_id") or ""
        description = annot.get("description") or ""
        suggested = annot.get("suggested_action") or ""
        affected_ip = annot.get("affected_ip") or ""
        parts = [alertname]
        if severity:
            parts.append(f"severity={severity}")
        if category:
            parts.append(f"category={category}")
        if incident_id:
            parts.append(f"incident={incident_id}")
        if ns:
            parts.append(f"namespace={ns}")
        if affected_ip:
            parts.append(f"affected_ip={affected_ip}")
        if description:
            parts.append(description[:400])
        if suggested:
            parts.append(f"suggested_action={suggested[:200]}")
        eh = " | ".join(parts)[:800]
        cq = _prometheus_canonical_document(labels, annot)
        gigo = build_gigo_metadata(labels, annot)
        return AnomalyEvent(
            trace_id=trace_id,
            rule_name=alertname,
            target="cluster",
            namespace=ns,
            metric_value=0.0,
            threshold=0.0,
            canonical_query=cq,
            timestamp=str(int(payload.get("received_at") or 0)),
            trigger_promql="",
            error_hint=eh,
            omni_layer="security",
            gigo_metadata=gigo,
        )

    if source in ("telegram", "telegram_callback"):
        text = str(payload.get("text") or "")
        return AnomalyEvent(
            trace_id=trace_id,
            rule_name="TelegramInbound",
            target="cluster",
            namespace="",
            metric_value=0.0,
            threshold=0.0,
            canonical_query=text[:2000],
            timestamp="",
            trigger_promql="",
            error_hint=text[:800],
            gigo_metadata={},
        )

    # Fallback: stringify payload for matrix catch-all
    raw = json.dumps(payload, ensure_ascii=False)[:2000]
    return AnomalyEvent(
        trace_id=trace_id,
        rule_name="GenericAlert",
        target="cluster",
        namespace="",
        metric_value=0.0,
        threshold=0.0,
        canonical_query=raw,
        timestamp="",
        trigger_promql="",
        error_hint=raw[:800],
        gigo_metadata={},
    )
