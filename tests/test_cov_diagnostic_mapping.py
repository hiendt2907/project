"""Tests for src/workers/diagnostic_mapping.py — coverage of uncovered paths."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("OMNI_ENV_MODE", "dev")

import pytest

from workers.diagnostic_mapping import (
    DiagnosticMatrixFile,
    MatrixRow,
    _row_matches,
    alertname_from_anomaly_event,
    classify_event,
    load_diagnostic_matrix,
)
from workers.proactive_models import AnomalyEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ev(error_hint: str = "", canonical_query: str = "x") -> AnomalyEvent:
    return AnomalyEvent(
        trace_id="test-1234",
        canonical_query=canonical_query,
        error_hint=error_hint,
    )


def _row(**kwargs) -> MatrixRow:
    defaults = dict(symptom_group="default", layer="k8s")
    defaults.update(kwargs)
    return MatrixRow(**defaults)


# ---------------------------------------------------------------------------
# load_diagnostic_matrix
# ---------------------------------------------------------------------------


def test_load_matrix_missing_file_returns_empty():
    result = load_diagnostic_matrix("/nonexistent/path/matrix.yaml")
    assert isinstance(result, DiagnosticMatrixFile)
    assert result.rows == []


def test_load_matrix_valid_yaml():
    content = """
version: 1
rows:
  - symptom_group: crash_loop
    layer: k8s
    priority: 10
    error_hint_pattern: "CrashLoop"
"""
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(content)
        path = f.name
    try:
        result = load_diagnostic_matrix(path)
        assert len(result.rows) == 1
        assert result.rows[0].symptom_group == "crash_loop"
        assert result.rows[0].priority == 10
    finally:
        os.unlink(path)


def test_load_matrix_empty_yaml_returns_default():
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write("")
        path = f.name
    try:
        result = load_diagnostic_matrix(path)
        assert isinstance(result, DiagnosticMatrixFile)
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# _row_matches — error_hint_pattern
# ---------------------------------------------------------------------------


def test_row_matches_error_hint_pattern_match():
    row = _row(error_hint_pattern="CrashLoop")
    ev = _ev(error_hint="pod in CrashLoopBackOff")
    assert _row_matches(row, ev) is True


def test_row_matches_error_hint_pattern_no_match():
    row = _row(error_hint_pattern="OOMKilled")
    ev = _ev(error_hint="CrashLoop")
    assert _row_matches(row, ev) is False


def test_row_matches_error_hint_invalid_regex():
    row = _row(error_hint_pattern="[invalid(")
    ev = _ev(error_hint="some hint")
    assert _row_matches(row, ev) is False


# ---------------------------------------------------------------------------
# _row_matches — canonical_query_pattern
# ---------------------------------------------------------------------------


def test_row_matches_canonical_query_pattern_match():
    row = _row(canonical_query_pattern="predict_linear")
    ev = _ev(canonical_query="rate(requests)[5m] predict_linear")
    assert _row_matches(row, ev) is True


def test_row_matches_canonical_query_pattern_no_match():
    row = _row(canonical_query_pattern="predict_linear")
    ev = _ev(canonical_query="some other query")
    assert _row_matches(row, ev) is False


def test_row_matches_canonical_query_invalid_regex():
    row = _row(canonical_query_pattern="[bad(")
    ev = _ev(canonical_query="any query")
    assert _row_matches(row, ev) is False


# ---------------------------------------------------------------------------
# _row_matches — both patterns (OR logic)
# ---------------------------------------------------------------------------


def test_row_matches_both_patterns_either_matches():
    row = _row(error_hint_pattern="CrashLoop", canonical_query_pattern="NO_MATCH_EVER")
    ev = _ev(error_hint="CrashLoopBackOff")
    assert _row_matches(row, ev) is True


def test_row_matches_both_patterns_neither_matches():
    row = _row(error_hint_pattern="NOPE", canonical_query_pattern="NOPE2")
    ev = _ev(error_hint="other", canonical_query="something else")
    assert _row_matches(row, ev) is False


# ---------------------------------------------------------------------------
# _row_matches — label predicates via JSON canonical_query
# ---------------------------------------------------------------------------


def _json_ev(labels: dict, error_hint: str = "") -> AnomalyEvent:
    cq = json.dumps({"labels": labels})
    return AnomalyEvent(trace_id="trace-json-001", canonical_query=cq, error_hint=error_hint)


def test_row_matches_alertname_label_match():
    row = _row(labels_alertname="KubePodCrashLooping")
    ev = _json_ev({"alertname": "KubePodCrashLooping"})
    assert _row_matches(row, ev) is True


def test_row_matches_alertname_label_no_match():
    row = _row(labels_alertname="KubePodCrashLooping")
    ev = _json_ev({"alertname": "SomeOtherAlert"})
    assert _row_matches(row, ev) is False


def test_row_matches_alertname_case_insensitive():
    row = _row(labels_alertname="kubepodcrashlooping")
    ev = _json_ev({"alertname": "KubePodCrashLooping"})
    assert _row_matches(row, ev) is True


def test_row_matches_domain_label_match():
    row = _row(labels_domain="security")
    ev = _json_ev({"domain": "security"})
    assert _row_matches(row, ev) is True


def test_row_matches_domain_label_absent_waived():
    # domain label not in event labels → predicate waived → falls through to patterns
    row = _row(labels_domain="security", error_hint_pattern="attack")
    ev = _json_ev({"alertname": "X"}, error_hint="DDoS attack detected")
    # domain absent in labels, error_hint matches
    assert _row_matches(row, ev) is True


def test_row_matches_workload_label_match():
    row = _row(labels_workload="nginx")
    ev = _json_ev({"workload": "nginx"})
    assert _row_matches(row, ev) is True


def test_row_matches_workload_via_deployment_key():
    row = _row(labels_workload="nginx")
    ev = _json_ev({"deployment": "nginx"})
    assert _row_matches(row, ev) is True


def test_row_matches_reason_pattern_match():
    row = _row(labels_reason_pattern="OOM.*")
    ev = _json_ev({"reason": "OOMKilled"})
    assert _row_matches(row, ev) is True


def test_row_matches_reason_pattern_no_match():
    row = _row(labels_reason_pattern="OOM.*")
    ev = _json_ev({"reason": "CrashLoop"})
    assert _row_matches(row, ev) is False


def test_row_matches_reason_invalid_regex():
    row = _row(labels_reason_pattern="[bad(")
    ev = _json_ev({"reason": "anything"})
    assert _row_matches(row, ev) is False


def test_row_matches_all_labels_present_and_match():
    row = _row(
        labels_alertname="Alert",
        labels_domain="infra",
    )
    ev = _json_ev({"alertname": "Alert", "domain": "infra"})
    assert _row_matches(row, ev) is True


def test_row_matches_all_labels_present_partial_mismatch():
    row = _row(labels_alertname="Alert", labels_domain="security")
    ev = _json_ev({"alertname": "Alert", "domain": "infra"})
    assert _row_matches(row, ev) is False


def test_row_no_predicates_no_patterns_returns_false():
    row = _row()
    ev = _ev(error_hint="anything", canonical_query="anything")
    assert _row_matches(row, ev) is False


def test_row_canonical_query_not_json_falls_through():
    row = _row(error_hint_pattern="crash")
    ev = _ev(error_hint="crash detected", canonical_query="not json at all")
    assert _row_matches(row, ev) is True


def test_row_canonical_query_invalid_json():
    row = _row(error_hint_pattern="crash")
    ev = _ev(error_hint="crash", canonical_query="{not valid json}")
    assert _row_matches(row, ev) is True


# ---------------------------------------------------------------------------
# alertname_from_anomaly_event
# ---------------------------------------------------------------------------


def test_alertname_from_json_canonical_query():
    ev = _json_ev({"alertname": "KubeNodeNotReady"})
    assert alertname_from_anomaly_event(ev) == "KubeNodeNotReady"


def test_alertname_from_non_json_query():
    ev = _ev(canonical_query="not a json string")
    assert alertname_from_anomaly_event(ev) == ""


def test_alertname_from_json_without_labels():
    ev = _ev(canonical_query=json.dumps({"other": "data"}))
    assert alertname_from_anomaly_event(ev) == ""


def test_alertname_from_json_missing_alertname():
    ev = _json_ev({"domain": "infra"})
    assert alertname_from_anomaly_event(ev) == ""


def test_alertname_from_json_array_not_dict():
    ev = _ev(canonical_query=json.dumps([1, 2, 3]))
    assert alertname_from_anomaly_event(ev) == ""


def test_alertname_from_json_labels_not_dict():
    ev = _ev(canonical_query=json.dumps({"labels": "not a dict"}))
    assert alertname_from_anomaly_event(ev) == ""


# ---------------------------------------------------------------------------
# classify_event
# ---------------------------------------------------------------------------


def test_classify_event_matches_first_priority_row():
    rows = [
        _row(symptom_group="generic", layer="k8s", priority=200, error_hint_pattern="crash"),
        _row(symptom_group="specific", layer="k8s", priority=10, error_hint_pattern="crash"),
    ]
    matrix = DiagnosticMatrixFile(rows=rows)
    ev = _ev(error_hint="pod crash detected")
    result = classify_event(ev, matrix)
    assert result is not None
    assert result.symptom_group == "specific"  # lower priority number wins


def test_classify_event_returns_none_when_no_match():
    matrix = DiagnosticMatrixFile(rows=[_row(error_hint_pattern="NEVER_MATCH")])
    ev = _ev(error_hint="something else")
    assert classify_event(ev, matrix) is None


def test_classify_event_empty_matrix():
    matrix = DiagnosticMatrixFile(rows=[])
    ev = _ev(error_hint="crash")
    assert classify_event(ev, matrix) is None


def test_classify_event_priority_sorting():
    rows = [
        _row(symptom_group="row_100", layer="k8s", priority=100, error_hint_pattern="oom"),
        _row(symptom_group="row_50", layer="k8s", priority=50, error_hint_pattern="oom"),
        _row(symptom_group="row_1", layer="k8s", priority=1, error_hint_pattern="oom"),
    ]
    matrix = DiagnosticMatrixFile(rows=rows)
    ev = _ev(error_hint="OOM kill / oom event")
    result = classify_event(ev, matrix)
    assert result.symptom_group == "row_1"
