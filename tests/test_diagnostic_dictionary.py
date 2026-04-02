"""Diagnostic matrix load + classify (no cluster)."""

from __future__ import annotations

from pathlib import Path

from workers.diagnostic_mapping import classify_event, load_diagnostic_matrix
from workers.proactive_models import AnomalyEvent


def test_load_matrix() -> None:
    root = Path(__file__).resolve().parents[1]
    m = load_diagnostic_matrix(root / "config" / "diagnostic_matrix.yaml")
    assert m.version == 1
    assert len(m.rows) >= 1


def test_classify_crash_hint() -> None:
    root = Path(__file__).resolve().parents[1]
    m = load_diagnostic_matrix(root / "config" / "diagnostic_matrix.yaml")
    ev = AnomalyEvent(
        trace_id="trace-1",
        canonical_query="sum(kube_pod_container_status_waiting_reason)",
        error_hint="crash_loop_backoff",
    )
    row = classify_event(ev, m)
    assert row is not None
    assert row.symptom_group == "crash_loop_backoff"
