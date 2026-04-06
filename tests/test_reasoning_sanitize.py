"""Sanitized analyst input + relevance guard."""

from __future__ import annotations

from pkg.reasoning.sanitize import (
    evidence_relevance_warning,
    format_batch_sanitized_analyst_user_text,
    format_sanitized_analyst_user_text,
)


def test_irrelevant_cpu_alert_vs_redis_probe() -> None:
    w = evidence_relevance_warning("nginx CPU ~90% saturation", "redis_ping")
    assert w is not None
    assert "workload resource" in w.lower() or "cpu" in w.lower()


def test_irrelevant_highcpu_label_vs_redis_probe() -> None:
    w = evidence_relevance_warning("HighCPU nginx-test CPU 90%", "redis_ping")
    assert w is not None


def test_relevant_redis_alert_vs_redis_probe() -> None:
    assert evidence_relevance_warning("redis PEL backlog", "redis_ping") is None


def test_format_sanitized_short() -> None:
    s = format_sanitized_analyst_user_text(
        {
            "alert_rule": "IngressPrometheus",
            "alert_hint": "HighCPU",
            "probe": "redis_ping",
            "result": "PASSED",
            "symptom_group": "g",
            "layer": "x",
            "raw": "ok",
            "ts": "1",
        }
    )
    assert "endpoint_hints" not in s
    assert "[ALERT_CONTEXT]" in s
    assert "[EVIDENCE]" in s
    assert "HighCPU" in s


def test_compact_labels_only_in_user_text() -> None:
    cq = '{"labels": {"alertname": "X", "pod": "p1"}, "annotations": {"summary": "s"}}'
    s = format_sanitized_analyst_user_text(
        {
            "canonical_query_snippet": cq,
            "probe": "redis_ping",
            "result": "PASSED",
            "ts": "1",
        }
    )
    assert "annotations" not in s
    assert '"alertname": "X"' in s or '"alertname":"X"' in s


def test_format_batch_two_probes() -> None:
    a = {
        "alert_rule": "IngressPrometheus",
        "alert_hint": "HighCPU",
        "probe": "k8s_clinical_pod_status",
        "result": "PASSED",
        "symptom_group": "workload_resource",
        "layer": "workload",
        "ts": "1",
    }
    b = {
        "alert_rule": "IngressPrometheus",
        "alert_hint": "HighCPU",
        "probe": "k8s_clinical_pod_metrics",
        "result": "PASSED",
        "symptom_group": "workload_resource",
        "layer": "workload",
        "raw": "nginx: cpu=0",
        "ts": "1",
    }
    s = format_batch_sanitized_analyst_user_text([a, b])
    assert "BATCH_DIAGNOSTIC_EVIDENCE" in s
    assert "k8s_clinical_pod_status" in s
    assert "k8s_clinical_pod_metrics" in s
