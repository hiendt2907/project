"""Tests for SIEM alert pipeline: alert_to_event source=siem path, synthetic evidence.

`translate_incident`/`workers.siem_bridge` removed 2026-08-10 (FinGuard→Smart SIEM internal
merge, plans/finguard-to-smart-siem-merge-2026-08-04.md phase S0.2) — it converted a FinGuard
Redis-stream incident into an Alertmanager-shaped envelope; that external source no longer
exists. `_build_siem_alert()` below reproduces just enough of the same envelope shape (now with
canonical `siem_source="omni_siem"` per S0.3, not the retired `"finguard"` literal) so the
still-live consumers (`alert_to_event.py`, `diagnostic_dispatcher.py`) stay covered.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from workers.alert_to_event import build_anomaly_event_from_alert_payload

_CATEGORY_TO_ALERTNAME = {
    "ddos": "SIEMDDoSDetected",
    "malware": "SIEMMalwareDetected",
    "data_exfil": "SIEMDataExfiltration",
    "k8s_threat": "SIEMKubernetesThreat",
}
_SEVERITY_MAP = {"critical": "critical", "high": "warning", "medium": "warning", "low": "info"}


def _build_siem_alert(msg_id: str, fields: dict) -> dict:
    """Minimal stand-in for the retired `translate_incident` — same envelope shape."""
    category = fields.get("category", "unknown").lower()
    severity = fields.get("severity", "medium").lower()
    incident_id = fields.get("id", "inc-unknown")
    trace_id = fields.get("trace_id") or f"omni-siem-{incident_id[:8]}"
    alert_name = _CATEGORY_TO_ALERTNAME.get(category, f"SIEM{category.title()}")
    return {
        "version": "4",
        "status": "firing",
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": alert_name,
                "severity": _SEVERITY_MAP.get(severity, "warning"),
                "source": fields.get("source", "omni_siem"),
                "siem_source": "omni_siem",
                "siem_tenant": fields.get("tenant_id", "unknown"),
                "siem_category": category,
                "siem_incident_id": incident_id,
                "trace_id": trace_id,
            },
            "annotations": {
                "description": fields.get("description", ""),
                "suggested_action": fields.get("suggested_action", ""),
                "affected_ip": fields.get("affected_ip", ""),
                "siem_stream_msg_id": msg_id,
            },
        }],
        "commonLabels": {"siem_source": "omni_siem"},
        "groupLabels": {"alertname": alert_name},
    }


def _make_siem_fields(category: str = "ddos", severity: str = "critical") -> dict:
    return {
        "id": "inc-abc12345",
        "severity": severity,
        "category": category,
        "tenant_id": "tenant-1",
        "description": f"Large-scale {category} detected targeting port 443",
        "suggested_action": "Block IP range and rate-limit",
        "affected_ip": "10.0.1.5",
    }


# ---------------------------------------------------------------------------
# alert_to_event: source=siem path builds rich AnomalyEvent
# ---------------------------------------------------------------------------

def _make_siem_alert_payload(category: str = "ddos", severity: str = "critical") -> dict:
    """Simulate what _process_stream_entry produces after decode_kafka_value_to_fields."""
    fields = _make_siem_fields(category=category, severity=severity)
    alert = _build_siem_alert("msg-3", fields)
    trace_id = alert["alerts"][0]["labels"].get("trace_id", "omni-siem-msg-3")
    return {
        "source": "siem",
        "trace_id": trace_id,
        "data": alert,
    }


def test_siem_source_not_generic_alert():
    payload = _make_siem_alert_payload()
    ev = build_anomaly_event_from_alert_payload(payload)
    assert ev.rule_name != "GenericAlert", f"SIEM alert should not become GenericAlert; got {ev.rule_name}"
    assert ev.rule_name.startswith("SIEM"), f"Expected SIEM prefix, got {ev.rule_name}"


def test_siem_event_has_rich_error_hint():
    payload = _make_siem_alert_payload(category="ddos", severity="critical")
    ev = build_anomaly_event_from_alert_payload(payload)
    assert "ddos" in ev.error_hint.lower() or "SIEMDDoS" in ev.error_hint, f"error_hint: {ev.error_hint!r}"
    assert "critical" in ev.error_hint.lower(), f"severity missing from hint: {ev.error_hint!r}"
    assert "10.0.1.5" in ev.error_hint, f"affected_ip missing from hint: {ev.error_hint!r}"
    assert "port 443" in ev.error_hint or "detected" in ev.error_hint.lower(), f"description missing: {ev.error_hint!r}"


def test_siem_event_has_security_layer():
    payload = _make_siem_alert_payload()
    ev = build_anomaly_event_from_alert_payload(payload)
    assert ev.omni_layer == "security", f"Expected omni_layer=security, got {ev.omni_layer!r}"


def test_siem_event_has_canonical_query():
    payload = _make_siem_alert_payload(category="malware")
    ev = build_anomaly_event_from_alert_payload(payload)
    cq = json.loads(ev.canonical_query)
    assert "labels" in cq and "siem_category" in cq["labels"], f"canonical_query missing siem_category: {ev.canonical_query}"
    assert cq["labels"]["siem_category"] == "malware"


def test_siem_trace_id_fallback_from_alert_labels_when_envelope_omits_top_level():
    """E2E harness: bridge always sets labels.trace_id; envelope top-level may be stripped by legacy paths."""
    payload = _make_siem_alert_payload()
    expected = payload["trace_id"]
    del payload["trace_id"]
    ev = build_anomaly_event_from_alert_payload(payload)
    assert ev.trace_id == expected


# ---------------------------------------------------------------------------
# diagnostic_dispatcher: SIEM synthetic evidence published
# ---------------------------------------------------------------------------

def _make_dispatcher_ctx(kafka_captures: list):
    kafka = MagicMock()
    kafka.send_dict = AsyncMock(side_effect=lambda topic, msg: kafka_captures.append({"topic": topic, "msg": msg}))
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = SimpleNamespace(
        diagnostic_dictionary_enabled=True,
        diagnostic_matrix_path="",
        kafka_topic_diagnostic_evidence="omni-diagnostic-evidence",
    )
    return SimpleNamespace(settings=settings, kafka=kafka, redis=redis)


@pytest.mark.asyncio
async def test_siem_dispatcher_publishes_synthetic_evidence():
    from workers.diagnostic_dispatcher import run_diagnostic_pipeline

    kafka_captures: list = []
    ctx = _make_dispatcher_ctx(kafka_captures)

    payload = _make_siem_alert_payload(category="ddos")
    ev = build_anomaly_event_from_alert_payload(payload)

    import unittest.mock as mock
    with (
        mock.patch("workers.diagnostic_dispatcher.load_diagnostic_matrix", return_value=[]),
        mock.patch("workers.diagnostic_dispatcher.register_diag_expected_probes", new_callable=AsyncMock),
    ):
        await run_diagnostic_pipeline(ctx, ev)

    evidence_msgs = [m for m in kafka_captures if m["topic"] == "omni-diagnostic-evidence"]
    assert evidence_msgs, "Expected synthetic evidence published to omni-diagnostic-evidence"
    body = json.loads(evidence_msgs[0]["msg"]["data"])
    assert body["probe"] == "siem_incident_context"
    assert body["evidence_source"] == "SIEM"
    assert body["layer"] == "security"
    ef = body.get("extracted_fact", {})
    assert ef.get("category") == "ddos", f"extracted_fact: {ef}"
    assert ef.get("severity") == "critical"
    assert ef.get("affected_ip") == "10.0.1.5"


@pytest.mark.asyncio
async def test_siem_dispatcher_skips_k8s_probes():
    """SIEM dispatcher path must NOT attempt to run K8s probes."""
    from workers.diagnostic_dispatcher import run_diagnostic_pipeline

    kafka_captures: list = []
    ctx = _make_dispatcher_ctx(kafka_captures)

    payload = _make_siem_alert_payload(category="data_exfil")
    ev = build_anomaly_event_from_alert_payload(payload)

    import unittest.mock as mock
    with (
        mock.patch("workers.diagnostic_dispatcher.load_diagnostic_matrix", return_value=[]),
        mock.patch("workers.diagnostic_dispatcher.register_diag_expected_probes", new_callable=AsyncMock),
        mock.patch("workers.diagnostic_dispatcher.run_probe", new_callable=AsyncMock) as mock_probe,
    ):
        await run_diagnostic_pipeline(ctx, ev)
    mock_probe.assert_not_called()
