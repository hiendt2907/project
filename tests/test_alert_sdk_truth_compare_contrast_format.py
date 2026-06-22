"""Structured contrast operator digest (deterministic, no LLM)."""

from __future__ import annotations

import json

from workers.alert_sdk_truth_compare import (
    build_contrast_diagnosis_for_action,
    build_contrast_operator_telegram_body,
)


def _sample_by_probe() -> dict[str, dict]:
    labels_obj = {
        "labels": {
            "alertname": "HighCPUUsage",
            "namespace": "multi-agent",
            "pod": "nginx-test-abc",
            "deployment": "nginx-test",
            "container": "nginx",
            "severity": "warning",
        },
        "annotations": {
            "summary": "nginx-test pod CPU ~90%",
            "description": "Container nginx reports high CPU vs limit",
        },
    }
    return {
        "k8s_clinical_pod_metrics": {
            "symptom_group": "workload_resource",
            "canonical_query_snippet": json.dumps(labels_obj),
            "extracted_fact": json.dumps(
                {"containers": [{"name": "nginx", "cpu": "0", "memory": "5768Ki"}]}
            ),
            "alert_rule": "IngressPrometheus",
            "alert_hint": "HighCPUUsage namespace=multi-agent pod=nginx-test-abc",
        },
        "k8s_clinical_pod_status": {
            "symptom_group": "workload_resource",
            "extracted_fact": json.dumps(
                {"pods": [{"pod": "nginx-test-abc", "namespace": "multi-agent", "phase": "Running"}]}
            ),
        },
        "prom_pod_cpu_cores": {
            "symptom_group": "workload_resource",
            "extracted_fact": json.dumps({"s0": 0.0, "unit": "cores_sum_rate5m"}),
            "raw": "sum(rate(container_cpu_usage_seconds_total[5m]))",
        },
    }


def test_operator_telegram_body_names_workload_and_includes_kubectl() -> None:
    body = build_contrast_operator_telegram_body(
        _sample_by_probe(),
        "Alert claims elevated workload CPU; SDK shows negligible.",
        "gw-prom-test",
        locale="en",
    )
    assert "multi-agent" in body
    assert "nginx-test" in body
    assert "nginx-test-abc" in body
    assert "HighCPUUsage" in body
    assert "Running" in body
    assert "kubectl" in body
    assert "gw-prom-test" in body
    assert "Alert claims elevated workload CPU" in body
    assert "trust the state machine" in body.lower()


def test_operator_body_locale_vi_has_vietnamese_headers() -> None:
    body = build_contrast_operator_telegram_body(
        _sample_by_probe(),
        "CPU alert vs SDK.",
        "t-vi",
        locale="vi",
    )
    assert "PHẠM VI / VỊ TRÍ" in body
    assert "ALERT ĐANG TUYÊN BỐ GÌ" in body
    assert "t-vi" in body


def test_operator_body_locale_both_includes_en_and_vi_markers() -> None:
    body = build_contrast_operator_telegram_body(
        _sample_by_probe(),
        "Narr.",
        "t-both",
        locale="both",
    )
    assert "WHO / WHERE (scope)" in body
    assert "[VI]" in body
    assert "PHẠM VI / VỊ TRÍ" in body


def test_diagnosis_for_action_prefixes_scope() -> None:
    d = build_contrast_diagnosis_for_action(
        _sample_by_probe(),
        "Narrative tail.",
    )
    assert "ns=multi-agent" in d
    assert "deploy=nginx-test" in d
    assert "pod=nginx-test-abc" in d
    assert "Tin vào state machine" in d
    assert "Narrative tail." in d
