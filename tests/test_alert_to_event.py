"""Tests for Prometheus → AnomalyEvent mapping (alert pipeline input)."""

from __future__ import annotations

import json

from workers.alert_to_event import build_anomaly_event_from_alert_payload


def test_prometheus_payload_stable_canonical_query_and_labels() -> None:
    payload = {
        "trace_id": "tr-1",
        "source": "prometheus",
        "received_at": 1700000000,
        "data": {
            "alerts": [
                {
                    "labels": {
                        "alertname": "ProbeFailureLab",
                        "namespace": "multi-agent",
                        "pod": "omni-prober-xyz",
                        "container": "worker",
                        "deployment": "omni-prober",
                        "domain": "lab",
                        "reason": "CrashLoopBackOff",
                    },
                    "annotations": {"summary": "pod unhealthy", "description": "backoff"},
                }
            ]
        },
    }
    ev = build_anomaly_event_from_alert_payload(payload)
    assert ev.namespace == "multi-agent"
    assert "ProbeFailureLab" in ev.error_hint
    assert "omni-prober-xyz" in ev.error_hint
    assert ev.canonical_query.startswith("{")
    doc = json.loads(ev.canonical_query)
    assert doc["labels"]["alertname"] == "ProbeFailureLab"
    assert doc["labels"]["pod"] == "omni-prober-xyz"
    # Sorted keys for stability
    keys = list(doc["labels"].keys())
    assert keys == sorted(keys)


def test_prometheus_empty_labels_graceful() -> None:
    payload = {
        "trace_id": "tr-2",
        "source": "prometheus",
        "data": {"alerts": [{"labels": {}, "annotations": {}}]},
    }
    ev = build_anomaly_event_from_alert_payload(payload)
    assert ev.canonical_query
    assert "unknown_alert" in ev.error_hint or "unknown" in ev.error_hint.lower()


def test_telegram_source_unchanged() -> None:
    payload = {"trace_id": "trace-tg-01", "source": "telegram", "text": "check pods"}
    ev = build_anomaly_event_from_alert_payload(payload)
    assert ev.rule_name == "TelegramInbound"
    assert "check pods" in ev.error_hint
