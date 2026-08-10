"""Tests for SIEM evidence envelope (SIEMEvidenceAdapter) lane/field contract.

Dual-emit + idempotency tests for `workers.siem_bridge` removed 2026-08-10 —
siem_bridge.py deleted as part of the FinGuard→Smart SIEM internal merge
(plans/finguard-to-smart-siem-merge-2026-08-04.md, phase S0.2): it read from
the finguard-customer namespace's Redis, which no longer exists. The ingest
path is now agent_webhook.py → omni-siem-raw directly (phase S2), covered by
tests/test_siem_ingest_routing.py.
"""

from __future__ import annotations


def _make_siem_fields(
    incident_id: str = "inc-abcd1234",
    category: str = "ddos",
    severity: str = "critical",
    tenant_id: str = "tenant-x",
    source_ip: str = "10.0.1.99",
) -> dict:
    return {
        "id": incident_id,
        "severity": severity,
        "category": category,
        "tenant_id": tenant_id,
        "description": f"Large-scale {category} detected",
        "suggested_action": "Block IP range",
        "affected_ip": source_ip,
        "source_ip": source_ip,
        "timestamp_unix": 1700000000,
    }


# ---------------------------------------------------------------------------
# Evidence envelope has SIEM_SECURITY lane fields
# ---------------------------------------------------------------------------

def test_siem_evidence_envelope_has_siem_lane():
    """SIEMEvidenceAdapter must set lane='SIEM_SECURITY' and stream_tags=['SIEM_SECURITY']."""
    from services.evidence_adapter.siem_adapter import SIEMEvidenceAdapter

    adapter = SIEMEvidenceAdapter()
    envelopes = adapter.to_evidence(_make_siem_fields())

    assert envelopes, "Expected at least one evidence envelope"
    primary = envelopes[0]
    assert primary.get("lane") == "SIEM_SECURITY", (
        f"Expected lane='SIEM_SECURITY', got {primary.get('lane')!r}"
    )
    assert primary.get("stream_tags") == ["SIEM_SECURITY"], (
        f"Expected stream_tags=['SIEM_SECURITY'], got {primary.get('stream_tags')!r}"
    )


def test_siem_network_envelope_also_has_siem_lane():
    """Network envelope (when affected_ip present) must also carry SIEM_SECURITY lane tags."""
    from services.evidence_adapter.siem_adapter import SIEMEvidenceAdapter

    adapter = SIEMEvidenceAdapter()
    envelopes = adapter.to_evidence(_make_siem_fields(source_ip="192.168.1.5"))

    # Network envelope is the second one (present because affected_ip is set)
    assert len(envelopes) == 2, "Expected primary + network envelopes when affected_ip present"
    network = envelopes[1]
    assert network.get("lane") == "SIEM_SECURITY"
    assert network.get("stream_tags") == ["SIEM_SECURITY"]


# ---------------------------------------------------------------------------
# Evidence envelope has required fields
# ---------------------------------------------------------------------------

def test_siem_evidence_envelope_has_required_fields():
    """Primary envelope must contain incident_id, category, severity, affected_ip."""
    from services.evidence_adapter.siem_adapter import SIEMEvidenceAdapter

    fields = _make_siem_fields(
        incident_id="inc-test-001",
        category="malware",
        severity="high",
        source_ip="172.16.0.5",
    )
    adapter = SIEMEvidenceAdapter()
    envelopes = adapter.to_evidence(fields)

    primary = envelopes[0]

    # Top-level envelope fields
    assert primary.get("trace_id"), "trace_id must be present"
    assert primary.get("probe"), "probe must be present"
    assert primary.get("alert_rule"), "alert_rule must be present"
    assert primary.get("alert_hint"), "alert_hint must be present"

    ef = primary.get("extracted_fact", {})
    assert ef.get("incident_id") == "inc-test-001", f"incident_id: {ef.get('incident_id')!r}"
    assert ef.get("category") == "malware", f"category: {ef.get('category')!r}"
    # severity is mapped through _SEVERITY_MAP (high → warning)
    assert ef.get("severity") == "warning", f"severity: {ef.get('severity')!r}"
    assert ef.get("affected_ip") == "172.16.0.5", f"affected_ip: {ef.get('affected_ip')!r}"


def test_siem_evidence_envelope_probe_is_siem_incident():
    """Primary probe ID must be siem_incident."""
    from services.evidence_adapter.siem_adapter import SIEMEvidenceAdapter

    adapter = SIEMEvidenceAdapter()
    envelopes = adapter.to_evidence(_make_siem_fields())
    assert envelopes[0]["probe"] == "siem_incident"
