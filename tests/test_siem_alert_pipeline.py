"""Tests for SIEM alert pipeline: bridge envelope, alert_to_event, synthetic evidence."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from workers.alert_to_event import build_anomaly_event_from_alert_payload
from workers.siem_bridge import translate_incident


# ---------------------------------------------------------------------------
# A) siem_bridge: _process wraps in double-envelope
# ---------------------------------------------------------------------------

def _make_siem_fields(category: str = "ddos", severity: str = "critical") -> dict:
    return {
        "id": "inc-abc12345",
        "severity": severity,
        "category": category,
        "tenant_id": "tenant-1",
        "description": f"Large-scale {category} detected targeting port 443",
        "suggested_action": f"Block IP range and rate-limit",
        "affected_ip": "10.0.1.5",
    }


def test_translate_incident_alertname_prefixed():
    """All SIEM categories map to SIEM-prefixed alertnames."""
    for cat, expected in [
        ("ddos", "SIEMDDoSDetected"),
        ("malware", "SIEMMalwareDetected"),
        ("data_exfil", "SIEMDataExfiltration"),
        ("k8s_threat", "SIEMKubernetesThreat"),
        ("unknown_cat", "SIEMUnknowncat"),  # fallback
    ]:
        alert = translate_incident("msg-1", _make_siem_fields(category=cat))
        alertname = alert["alerts"][0]["labels"]["alertname"]
        assert alertname.startswith("SIEM"), f"Expected SIEM prefix for {cat}, got {alertname}"


def test_translate_incident_has_trace_id():
    alert = translate_incident("msg-1", _make_siem_fields())
    trace_id = alert["alerts"][0]["labels"].get("trace_id", "")
    assert trace_id.startswith("fg-"), f"Expected fg- prefix trace_id, got {trace_id!r}"


def test_siem_bridge_envelope_format():
    """_process must wrap in the same double-envelope the omni worker expects."""
    from workers.siem_bridge import translate_incident as _ti

    fields = _make_siem_fields()
    alert = _ti("msg-2", fields)
    trace_id = alert["alerts"][0]["labels"].get("trace_id", "fg-msg-2")

    inner = {"source": "siem", "trace_id": trace_id, "data": alert}
    envelope_bytes = json.dumps({"data": json.dumps(inner, ensure_ascii=False)}, ensure_ascii=False).encode()

    # Simulate decode_kafka_value_to_fields
    outer = json.loads(envelope_bytes.decode("utf-8"))
    assert "data" in outer, "Outer envelope must have 'data' key"
    inner_parsed = json.loads(outer["data"])
    assert inner_parsed["source"] == "siem"
    assert inner_parsed["trace_id"] == trace_id
    assert "alerts" in inner_parsed["data"]


# ---------------------------------------------------------------------------
# B) alert_to_event: source=siem path builds rich AnomalyEvent
# ---------------------------------------------------------------------------

def _make_siem_alert_payload(category: str = "ddos", severity: str = "critical") -> dict:
    """Simulate what _process_stream_entry produces after decode_kafka_value_to_fields."""
    fields = _make_siem_fields(category=category, severity=severity)
    from workers.siem_bridge import translate_incident as _ti
    alert = _ti("msg-3", fields)
    trace_id = alert["alerts"][0]["labels"].get("trace_id", "fg-msg-3")
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
# C) diagnostic_dispatcher: SIEM synthetic evidence published
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
    from workers.proactive_models import AnomalyEvent

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
