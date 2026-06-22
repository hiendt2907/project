"""Unit tests for services.evidence_adapter — protocol, adapter, and worker."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# worker.py checks env at module level — set before import
os.environ.setdefault("ADAPTER_REDIS_URL", "redis://localhost:6379")

from services.evidence_adapter.protocol import EvidenceAdapter
from services.evidence_adapter.siem_adapter import SIEMEvidenceAdapter


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------

class _MinimalAdapter:
    def to_evidence(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"trace_id": "t", "probe": "test", "alert_rule": "R", "alert_hint": "H", "extracted_fact": {}, "raw": ""}]


def test_minimal_adapter_satisfies_protocol():
    assert isinstance(_MinimalAdapter(), EvidenceAdapter)


def test_non_compliant_class_fails_protocol_check():
    class Bad:
        pass
    assert not isinstance(Bad(), EvidenceAdapter)


# ---------------------------------------------------------------------------
# SIEMEvidenceAdapter
# ---------------------------------------------------------------------------

def _make_incident(**overrides) -> dict[str, Any]:
    base = {
        "incident_id": "inc-001",
        "category": "ddos",
        "severity": "critical",
        "description": "DDoS flood detected",
        "namespace": "multi-agent",
        "tenant_id": "tenant-1",
        "source_ip": "1.2.3.4",
        "alert_name": "DDoSFlood",
        "data": json.dumps({"detail": "high pps"}),
    }
    base.update(overrides)
    return base


def test_siem_adapter_returns_list():
    adapter = SIEMEvidenceAdapter()
    envelopes = adapter.to_evidence(_make_incident())
    assert isinstance(envelopes, list)
    assert len(envelopes) >= 1


def test_siem_adapter_envelope_has_required_keys():
    adapter = SIEMEvidenceAdapter()
    env = adapter.to_evidence(_make_incident())[0]
    for key in ("trace_id", "probe", "alert_rule", "alert_hint", "extracted_fact", "raw"):
        assert key in env, f"missing key: {key}"


def test_siem_adapter_trace_id_is_string():
    adapter = SIEMEvidenceAdapter()
    env = adapter.to_evidence(_make_incident())[0]
    assert isinstance(env["trace_id"], str)
    assert len(env["trace_id"]) > 0


def test_siem_adapter_preserves_namespace():
    adapter = SIEMEvidenceAdapter()
    env = adapter.to_evidence(_make_incident(namespace="finguard-customer"))[0]
    assert "finguard-customer" in json.dumps(env)


def test_siem_adapter_handles_missing_optional_fields():
    adapter = SIEMEvidenceAdapter()
    minimal = {"incident_id": "x", "category": "auth_failure", "severity": "high"}
    envelopes = adapter.to_evidence(minimal)
    assert len(envelopes) >= 1


def test_siem_adapter_extracted_fact_is_dict():
    adapter = SIEMEvidenceAdapter()
    env = adapter.to_evidence(_make_incident())[0]
    assert isinstance(env["extracted_fact"], dict)


# ---------------------------------------------------------------------------
# AdapterGeneratorWorker
# ---------------------------------------------------------------------------

def test_worker_rejects_non_adapter():
    from services.evidence_adapter.worker import AdapterGeneratorWorker
    with pytest.raises(TypeError, match="EvidenceAdapter"):
        AdapterGeneratorWorker(object())  # type: ignore[arg-type]


def test_worker_accepts_valid_adapter():
    from services.evidence_adapter.worker import AdapterGeneratorWorker
    worker = AdapterGeneratorWorker(SIEMEvidenceAdapter())
    assert worker is not None


@pytest.mark.asyncio
async def test_worker_handle_acks_after_emit():
    from services.evidence_adapter.worker import AdapterGeneratorWorker

    adapter = SIEMEvidenceAdapter()
    worker = AdapterGeneratorWorker(adapter)

    redis = AsyncMock()
    producer = AsyncMock()

    incident = _make_incident()
    fields = {k: json.dumps(v) if isinstance(v, dict) else str(v) for k, v in incident.items()}

    with patch.object(adapter, "to_evidence", return_value=[
        {"trace_id": "t1", "probe": "siem", "alert_rule": "R", "alert_hint": "H", "extracted_fact": {}, "raw": "raw"}
    ]):
        await worker._handle(redis, producer, "msg-1", fields)

    producer.send_and_wait.assert_awaited_once()
    redis.xack.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_handle_skips_ack_on_error():
    from services.evidence_adapter.worker import AdapterGeneratorWorker

    adapter = SIEMEvidenceAdapter()
    worker = AdapterGeneratorWorker(adapter)

    redis = AsyncMock()
    producer = AsyncMock()
    producer.send_and_wait.side_effect = Exception("kafka down")

    with patch.object(adapter, "to_evidence", return_value=[
        {"trace_id": "t1", "probe": "siem", "alert_rule": "R", "alert_hint": "H", "extracted_fact": {}, "raw": ""}
    ]):
        await worker._handle(redis, producer, "msg-2", {})

    redis.xack.assert_not_awaited()
