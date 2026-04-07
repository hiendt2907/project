from __future__ import annotations

import json

from workers.diagnostic_mapping import DiagnosticMatrixFile, MatrixRow, classify_event
from workers.proactive_models import AnomalyEvent


def _ev(labels: dict[str, str], hint: str = "") -> AnomalyEvent:
    return AnomalyEvent(
        trace_id="t-map-1",
        canonical_query=json.dumps({"labels": labels}),
        error_hint=hint,
    )


def test_classify_event_label_first_probe_failure_lab() -> None:
    matrix = DiagnosticMatrixFile(
        rows=[
            MatrixRow(
                symptom_group="ollama_500_context",
                layer="application",
                priority=30,
                labels_alertname="Ollama500Context",
                error_hint_pattern="(?i)ollama|500",
            ),
            MatrixRow(
                symptom_group="crash_loop_backoff",
                layer="infrastructure",
                priority=10,
                labels_alertname="ProbeFailureLab",
                labels_domain="lab",
                labels_reason_pattern="(?i)waiting|backoff|crash",
            ),
        ]
    )
    ev = _ev(
        {
            "alertname": "ProbeFailureLab",
            "domain": "lab",
            "reason": "CreateContainerConfigError waiting",
        },
        hint="context cache failed",
    )
    row = classify_event(ev, matrix)
    assert row is not None
    assert row.symptom_group == "crash_loop_backoff"


def test_classify_event_priority_when_both_match() -> None:
    matrix = DiagnosticMatrixFile(
        rows=[
            MatrixRow(symptom_group="generic_unclassified", layer="unknown", priority=999, error_hint_pattern=".*"),
            MatrixRow(symptom_group="redis_streams_stuck", layer="infrastructure", priority=40, error_hint_pattern="(?i)redis"),
        ]
    )
    ev = _ev({"alertname": "CustomAlert"}, hint="redis lock delayed")
    row = classify_event(ev, matrix)
    assert row is not None
    assert row.symptom_group == "redis_streams_stuck"
