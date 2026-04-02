"""Map inbound alert payloads (gateway / telegram) to AnomalyEvent for diagnostic probes."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from workers.proactive_models import AnomalyEvent

logger = logging.getLogger(__name__)


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
        labels = a0.get("labels") if isinstance(a0.get("labels"), dict) else {}
        annot = a0.get("annotations") if isinstance(a0.get("annotations"), dict) else {}
        alertname = str(labels.get("alertname") or "unknown_alert")
        ns = str(labels.get("namespace") or "")
        eh = f"{alertname} {str(annot.get('description') or annot.get('summary') or '')[:400]}"
        cq = json.dumps({"labels": labels, "annotations": annot}, ensure_ascii=False)[:2000]
        return AnomalyEvent(
            trace_id=trace_id,
            rule_name="IngressPrometheus",
            target="cluster",
            namespace=ns,
            metric_value=0.0,
            threshold=0.0,
            canonical_query=cq,
            timestamp=str(int(payload.get("received_at") or 0)),
            trigger_promql="",
            error_hint=eh[:800],
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
    )
