"""Tests for SIEM structured advisory: what/who/why/how-to + forecast timeline."""

from __future__ import annotations

import json
import pytest

from workers.evidence_consumer import (
    _siem_diagnosis_from_batch,
    _siem_forecast_timeline,
    _format_siem_forecast_text,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _batch(category: str = "ddos", severity: str = "critical", ns: str = "prod-ns") -> list[dict]:
    siem_labels = {
        "alertname": f"SIEM{category.title()}",
        "siem_category": category,
        "siem_incident_id": "inc-001",
        "severity": severity,
        "namespace": ns,
    }
    return [{
        "probe": "siem_incident_context",
        "evidence_source": "SIEM",
        "alert_rule": f"SIEM{category.title()}",
        "alert_hint": f"SIEM alert category={category}",
        "canonical_query_snippet": json.dumps({"labels": siem_labels}),
        "extracted_fact": {
            "category": category,
            "severity": severity,
            "incident_id": "inc-001",
            "tenant": "t1",
            "description": f"Large-scale {category} attack detected.",
            "suggested_action": "Isolate and investigate",
            "affected_ip": "10.0.1.5",
            "namespace": ns,
        },
    }]


def _siem_labels(category: str, severity: str, ns: str) -> dict:
    return {"siem_category": category, "severity": severity, "namespace": ns, "siem_incident_id": "inc-001"}


# ---------------------------------------------------------------------------
# _siem_forecast_timeline
# ---------------------------------------------------------------------------

EXPECTED_TIMEFRAMES = {"1h", "3h", "6h", "12h", "24h"}

@pytest.mark.parametrize("category,severity", [
    ("ddos", "critical"),
    ("malware", "critical"),
    ("data_exfil", "critical"),
    ("k8s_threat", "critical"),
    ("auth_failure", "critical"),
    ("auth_failure", "high"),
    ("lateral_movement", "critical"),
    ("network_anomaly", "critical"),
    ("unknown_category", "critical"),  # default fallback
    ("ddos", "low"),  # low severity → falls back to critical entry
])
def test_forecast_has_all_five_timeframes(category: str, severity: str) -> None:
    forecast = _siem_forecast_timeline(category, severity)
    assert len(forecast) == 5, f"Expected 5 timeframes, got {len(forecast)} for {category}/{severity}"
    timeframes = {f["timeframe"] for f in forecast}
    assert timeframes == EXPECTED_TIMEFRAMES, f"Missing timeframes: {EXPECTED_TIMEFRAMES - timeframes}"


def test_forecast_fields_present() -> None:
    forecast = _siem_forecast_timeline("ddos", "critical")
    for f in forecast:
        assert "timeframe" in f
        assert "severity" in f
        assert "prediction" in f
        assert "confidence" in f
        assert f["confidence"] in ("high", "medium", "low")
        assert f["severity"] in ("degraded", "critical", "catastrophic", "healthy")


def test_forecast_ddos_critical_worsens_over_time() -> None:
    """DDoS critical: 1h degraded/critical → 24h catastrophic."""
    forecast = _siem_forecast_timeline("ddos", "critical")
    tf_map = {f["timeframe"]: f["severity"] for f in forecast}
    assert tf_map["24h"] == "catastrophic"
    assert tf_map["1h"] in ("degraded", "critical")


def test_forecast_auth_high_less_severe_than_critical() -> None:
    """Auth failure: both high and critical start degraded at 1h (brute-force slow-burn),
    but critical escalates to catastrophic faster (12h) vs high (stays critical longer)."""
    fc_high = _siem_forecast_timeline("auth_failure", "high")
    fc_crit = _siem_forecast_timeline("auth_failure", "critical")
    tf_high = {f["timeframe"]: f["severity"] for f in fc_high}
    tf_crit = {f["timeframe"]: f["severity"] for f in fc_crit}
    # Both start degraded (credential attacks take time to escalate)
    assert tf_high["1h"] == "degraded"
    assert tf_crit["1h"] == "degraded"
    # Critical ends worse: catastrophic at 24h
    assert tf_crit["24h"] == "catastrophic"
    # High ends critical (not catastrophic within 24h without MFA)
    assert tf_high["24h"] == "critical"


# ---------------------------------------------------------------------------
# _format_siem_forecast_text
# ---------------------------------------------------------------------------

def test_format_forecast_text_contains_timeframes() -> None:
    forecast = _siem_forecast_timeline("ddos", "critical")
    text = _format_siem_forecast_text(forecast)
    for tf in EXPECTED_TIMEFRAMES:
        assert tf in text, f"Timeframe {tf} missing in forecast text"


def test_format_forecast_text_severity_uppercase() -> None:
    forecast = _siem_forecast_timeline("malware", "critical")
    text = _format_siem_forecast_text(forecast)
    assert "[CATASTROPHIC]" in text or "[CRITICAL]" in text


# ---------------------------------------------------------------------------
# _siem_diagnosis_from_batch — structured sections
# ---------------------------------------------------------------------------

def test_diagnosis_has_what_section() -> None:
    batch = _batch("ddos", "critical", "prod-ns")
    labels = _siem_labels("ddos", "critical", "prod-ns")
    diag = _siem_diagnosis_from_batch(batch, labels, "")
    assert "WHAT:" in diag


def test_diagnosis_has_who_section() -> None:
    batch = _batch("malware", "critical", "blue-ns")
    labels = _siem_labels("malware", "critical", "blue-ns")
    diag = _siem_diagnosis_from_batch(batch, labels, "")
    assert "WHO:" in diag
    assert "blue-ns" in diag


def test_diagnosis_has_why_section() -> None:
    batch = _batch("k8s_threat", "critical", "kube-system")
    labels = _siem_labels("k8s_threat", "critical", "kube-system")
    diag = _siem_diagnosis_from_batch(batch, labels, "")
    assert "WHY:" in diag


def test_diagnosis_has_howto_section() -> None:
    batch = _batch("data_exfil", "critical", "ns1")
    labels = _siem_labels("data_exfil", "critical", "ns1")
    diag = _siem_diagnosis_from_batch(batch, labels, "")
    assert "HOW-TO" in diag


def test_diagnosis_has_forecast_section() -> None:
    batch = _batch("ddos", "critical", "prod-ns")
    labels = _siem_labels("ddos", "critical", "prod-ns")
    diag = _siem_diagnosis_from_batch(batch, labels, "")
    assert "Dự báo" in diag
    assert "+1h" in diag
    assert "+24h" in diag


def test_diagnosis_contains_incident_id() -> None:
    batch = _batch("ddos")
    labels = _siem_labels("ddos", "critical", "ns")
    diag = _siem_diagnosis_from_batch(batch, labels, "")
    assert "inc-001" in diag


def test_diagnosis_contains_affected_ip() -> None:
    batch = _batch("ddos")
    labels = _siem_labels("ddos", "critical", "ns")
    diag = _siem_diagnosis_from_batch(batch, labels, "")
    assert "10.0.1.5" in diag


def test_diagnosis_no_injection_in_namespace() -> None:
    """Namespace with braces must not cause format-string errors."""
    evil_ns = "ns-{rm -rf /}"
    batch = _batch("ddos", "critical", evil_ns)
    labels = _siem_labels("ddos", "critical", evil_ns)
    # Must not raise
    diag = _siem_diagnosis_from_batch(batch, labels, "")
    assert "HOW-TO" in diag
    # Braces stripped
    assert "{" not in diag.split("HOW-TO", 1)[1].split("Forecast", 1)[0]


def test_diagnosis_unknown_category_uses_default() -> None:
    batch = _batch("unknown_threat", "high", "ns")
    labels = _siem_labels("unknown_threat", "high", "ns")
    diag = _siem_diagnosis_from_batch(batch, labels, "")
    # Must still have all sections
    assert "WHAT:" in diag
    assert "WHO:" in diag
    assert "WHY:" in diag
    assert "HOW-TO" in diag
    assert "Dự báo" in diag


def test_diagnosis_no_auto_execute() -> None:
    batch = _batch("ddos")
    labels = _siem_labels("ddos", "critical", "ns")
    diag = _siem_diagnosis_from_batch(batch, labels, "")
    assert "NOT auto-execute" in diag
