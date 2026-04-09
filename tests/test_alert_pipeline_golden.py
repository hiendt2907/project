"""Golden-path checks for alert → matrix → RAG hints (no cluster)."""

from __future__ import annotations

import json
from pathlib import Path

from pkg.rag.gate import normalize_rag_query
from workers.alert_to_event import build_anomaly_event_from_alert_payload
from workers.diagnostic_mapping import classify_event, load_diagnostic_matrix
from workers.evidence_consumer import _hints_from_evidence_batch
from workers.proactive_models import AnomalyEvent


def _matrix() -> object:
    root = Path(__file__).resolve().parents[1]
    return load_diagnostic_matrix(root / "config" / "diagnostic_matrix.yaml")


def test_golden_probe_failure_lab_classifies_crash_row() -> None:
    payload = {
        "trace_id": "golden-pfl-001",
        "source": "prometheus",
        "received_at": 1,
        "data": {
            "alerts": [
                {
                    "labels": {
                        "alertname": "ProbeFailureLab",
                        "namespace": "multi-agent",
                        "domain": "lab",
                        "reason": "CrashLoopBackOff",
                    },
                    "annotations": {"summary": "pod unstable"},
                }
            ]
        },
    }
    ev = build_anomaly_event_from_alert_payload(payload)
    row = classify_event(ev, _matrix())
    assert row is not None
    assert row.symptom_group == "crash_loop_backoff"


def test_golden_hints_from_batch_include_alert_and_symptom() -> None:
    batch = [
        {
            "probe": "k8s_clinical_pod_status",
            "alert_rule": "HighCPU",
            "symptom_group": "workload_resource",
            "layer": "workload",
        }
    ]
    text = "[ALERT_CONTEXT]\n  rule: HighCPU\n  symptom_group: workload_resource\n"
    h = _hints_from_evidence_batch(batch, text)
    assert h is not None
    assert h.get("alertname") == "HighCPU"
    assert h.get("symptom_group") == "workload_resource"


def test_hints_from_batch_include_diagnostic_pattern_when_matrix_matches() -> None:
    from pkg.reasoning.incident_matrix_profile import invalidate_matrix_cache

    invalidate_matrix_cache()
    batch = [
        {
            "probe": "batch_diagnostic_evidence",
            "canonical_query_snippet": json.dumps(
                {
                    "labels": {
                        "alertname": "NginxTestContainerWaitingFaultLab",
                        "reason": "CreateContainerError",
                    }
                }
            ),
        }
    ]
    h = _hints_from_evidence_batch(batch, "")
    assert h is not None
    assert h.get("diagnostic_pattern") == "config_integrity"


def test_normalize_rag_query_prefixes_symptom_group() -> None:
    raw = "[RAG_QUERY]\nalert_name=Z\n"
    hints = {"namespace": "ns1", "alertname": "Z", "symptom_group": "workload_resource"}
    q = normalize_rag_query(raw, hints)
    assert "symptom_group=workload_resource" in q
    assert "namespace=ns1" in q


def test_normalize_rag_query_prefixes_diagnostic_pattern() -> None:
    raw = "[RAG_QUERY]\n"
    hints = {"alertname": "Z", "diagnostic_pattern": "config_integrity"}
    q = normalize_rag_query(raw, hints)
    assert "diagnostic_pattern=config_integrity" in q


def test_matrix_generic_catch_all_last() -> None:
    """Ensure catch-all row exists for unknown alerts."""
    m = _matrix()
    ev = AnomalyEvent(
        trace_id="golden-unknown-1",
        canonical_query=json.dumps({"labels": {"alertname": "WeirdUnknownAlert"}}),
        error_hint="something odd",
    )
    row = classify_event(ev, m)
    assert row is not None
    assert row.symptom_group == "generic_unclassified"
