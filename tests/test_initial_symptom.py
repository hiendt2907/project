"""Unit tests for InitialSymptom mappers."""

from __future__ import annotations

import json

from workers.memory.initial_symptom import (
    InitialSymptom,
    initial_symptom_from_alertmanager_alert,
    initial_symptom_from_evidence_batch,
)


def test_from_alertmanager_minimal():
    alert = {
        "labels": {"alertname": "PodCrashLooping", "namespace": "ns1", "severity": "warning"},
        "annotations": {"summary": "pod restarting", "description": "details"},
        "startsAt": "2024-01-01T00:00:00Z",
        "fingerprint": "abc",
    }
    s = initial_symptom_from_alertmanager_alert(alert)
    assert s.alertname == "PodCrashLooping"
    assert s.namespace == "ns1"
    assert s.severity == "warning"
    assert s.summary == "pod restarting"
    assert s.description == "details"
    assert "alertname" in s.render_for_prompt()


def test_from_evidence_batch_canonical_json():
    cq = json.dumps(
        {
            "labels": {
                "alertname": "KubeDeploymentReplicasMismatch",
                "namespace": "multi-agent",
            },
            "annotations": {"summary": "deploy unhealthy"},
        }
    )
    batch = [
        {
            "canonical_query_snippet": cq,
            "alert_rule": "deployment.rules",
            "alert_hint": "extra hint",
        }
    ]
    s = initial_symptom_from_evidence_batch(batch)
    assert s is not None
    assert s.alertname == "KubeDeploymentReplicasMismatch"
    assert s.alert_rule == "deployment.rules"


def test_from_evidence_batch_labels_only():
    cq = json.dumps({"labels": {"alertname": "TestAlert", "severity": "critical"}})
    batch = [{"canonical_query_snippet": cq, "probe": "x"}]
    s = initial_symptom_from_evidence_batch(batch)
    assert s is not None
    assert s.alertname == "TestAlert"


def test_empty_batch_returns_none():
    assert initial_symptom_from_evidence_batch([]) is None


def test_initial_symptom_model_dump_json_roundtrip():
    sym = InitialSymptom(alertname="X", alert_rule="r", labels={"a": "b"})
    raw = sym.model_dump(mode="json")
    sym2 = InitialSymptom.model_validate(raw)
    assert sym2.alertname == "X"
    assert sym2.labels == {"a": "b"}
