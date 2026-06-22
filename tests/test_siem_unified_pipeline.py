"""Tests for SIEM unified pipeline: dual-emit default, evidence envelope lane fields."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
# 1. DUAL_EMIT defaults to True
# ---------------------------------------------------------------------------

def test_siem_bridge_dual_emit_default():
    """SIEM_BRIDGE_DUAL_EMIT should default to True without any env override."""
    import importlib
    import sys

    # Remove cached module so we re-evaluate module-level DUAL_EMIT
    mod_name = "workers.siem_bridge"
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    # Ensure env var is absent (default should be true)
    import os
    env_backup = os.environ.pop("SIEM_BRIDGE_DUAL_EMIT", None)
    try:
        import workers.siem_bridge as bridge
        assert bridge.DUAL_EMIT is True, (
            f"DUAL_EMIT should default to True, got {bridge.DUAL_EMIT}"
        )
    finally:
        if env_backup is not None:
            os.environ["SIEM_BRIDGE_DUAL_EMIT"] = env_backup
        # Restore fresh import for other tests
        if mod_name in sys.modules:
            del sys.modules[mod_name]


def test_siem_bridge_dual_emit_explicit_false():
    """SIEM_BRIDGE_DUAL_EMIT=false should disable dual-emit."""
    import sys
    import os

    mod_name = "workers.siem_bridge"
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    env_backup = os.environ.get("SIEM_BRIDGE_DUAL_EMIT")
    os.environ["SIEM_BRIDGE_DUAL_EMIT"] = "false"
    try:
        import workers.siem_bridge as bridge
        assert bridge.DUAL_EMIT is False
    finally:
        if env_backup is None:
            os.environ.pop("SIEM_BRIDGE_DUAL_EMIT", None)
        else:
            os.environ["SIEM_BRIDGE_DUAL_EMIT"] = env_backup
        if mod_name in sys.modules:
            del sys.modules[mod_name]


# ---------------------------------------------------------------------------
# 2. Evidence envelope has SIEM_SECURITY lane fields
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
# 3. Evidence envelope has required fields
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


# ---------------------------------------------------------------------------
# 4. Dual-emit publishes to both omni-alerts and omni-siem-raw
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dual_emit_publishes_to_both_topics():
    """With DUAL_EMIT=True, _process must send to both omni-alerts AND omni-siem-raw."""
    import workers.siem_bridge as bridge

    # Capture send_and_wait calls
    calls: list[dict] = []

    mock_producer = MagicMock()
    mock_producer.send_and_wait = AsyncMock(
        side_effect=lambda topic, value: calls.append({"topic": topic, "value": value})
    )

    mock_redis = MagicMock()
    mock_redis.xack = AsyncMock(return_value=1)

    fields = _make_siem_fields()
    original_dual = bridge.DUAL_EMIT
    bridge.DUAL_EMIT = True
    try:
        await bridge._process(mock_redis, mock_producer, "msg-001", fields)
    finally:
        bridge.DUAL_EMIT = original_dual

    topics_sent = [c["topic"] for c in calls]
    assert bridge.KAFKA_TOPIC in topics_sent, (
        f"Expected {bridge.KAFKA_TOPIC} in topics; got {topics_sent}"
    )
    assert bridge.KAFKA_TOPIC_SIEM_RAW in topics_sent, (
        f"Expected {bridge.KAFKA_TOPIC_SIEM_RAW} in topics; got {topics_sent}"
    )
    assert len(calls) == 2, f"Expected exactly 2 sends with DUAL_EMIT=True; got {len(calls)}"


@pytest.mark.asyncio
async def test_single_emit_when_dual_emit_disabled():
    """With DUAL_EMIT=False, _process must send ONLY to omni-alerts."""
    import workers.siem_bridge as bridge

    calls: list[dict] = []

    mock_producer = MagicMock()
    mock_producer.send_and_wait = AsyncMock(
        side_effect=lambda topic, value: calls.append({"topic": topic, "value": value})
    )

    mock_redis = MagicMock()
    mock_redis.xack = AsyncMock(return_value=1)

    fields = _make_siem_fields()
    original_dual = bridge.DUAL_EMIT
    bridge.DUAL_EMIT = False
    try:
        await bridge._process(mock_redis, mock_producer, "msg-002", fields)
    finally:
        bridge.DUAL_EMIT = original_dual

    topics_sent = [c["topic"] for c in calls]
    assert bridge.KAFKA_TOPIC in topics_sent, f"omni-alerts must always receive messages"
    assert bridge.KAFKA_TOPIC_SIEM_RAW not in topics_sent, (
        "omni-siem-raw must NOT receive messages when DUAL_EMIT=False"
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_dual_emit_raw_envelope_contains_expected_keys():
    """The raw envelope sent to omni-siem-raw must contain schema_version and trace_id."""
    import workers.siem_bridge as bridge

    raw_payloads: list[bytes] = []

    mock_producer = MagicMock()
    async def _capture(topic: str, value: bytes) -> None:
        if topic == bridge.KAFKA_TOPIC_SIEM_RAW:
            raw_payloads.append(value)

    mock_producer.send_and_wait = AsyncMock(side_effect=_capture)
    mock_redis = MagicMock()
    mock_redis.xack = AsyncMock(return_value=1)

    fields = _make_siem_fields(incident_id="inc-xyz789")
    original_dual = bridge.DUAL_EMIT
    bridge.DUAL_EMIT = True
    try:
        await bridge._process(mock_redis, mock_producer, "msg-003", fields)
    finally:
        bridge.DUAL_EMIT = original_dual

    assert raw_payloads, "Expected at least one raw payload to omni-siem-raw"
    raw = json.loads(raw_payloads[0].decode("utf-8"))
    assert raw.get("schema_version") == "1.0.0", f"schema_version: {raw.get('schema_version')!r}"
    assert raw.get("trace_id", "").startswith("fg-"), f"trace_id: {raw.get('trace_id')!r}"
    assert raw.get("id") == "inc-xyz789", f"id: {raw.get('id')!r}"


@pytest.mark.asyncio
async def test_synthetic_events_are_dropped():
    """Synthetic events (autonomy_loop reason) must be ACK'd and dropped without Kafka send."""
    import workers.siem_bridge as bridge

    calls: list[dict] = []
    mock_producer = MagicMock()
    mock_producer.send_and_wait = AsyncMock(
        side_effect=lambda topic, value: calls.append({"topic": topic})
    )

    acked: list = []
    mock_redis = MagicMock()
    mock_redis.xack = AsyncMock(side_effect=lambda *a: acked.append(a))

    synthetic_fields = {**_make_siem_fields(), "reason": "autonomy_loop_test"}
    await bridge._process(mock_redis, mock_producer, "msg-synthetic", synthetic_fields)

    assert not calls, "Synthetic events must not be forwarded to Kafka"
    assert acked, "Synthetic events must be ACK'd"
