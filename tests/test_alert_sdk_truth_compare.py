"""Alert claim vs Kubernetes API state machine (PodMetrics / PodStatus)."""

from __future__ import annotations

import json

from workers.alert_sdk_truth_compare import compare_alert_claim_to_sdk_state


def test_contrast_when_alert_claim_not_matching_sdk_cpu_zero() -> None:
    by_probe = {
        "k8s_clinical_pod_status": {
            "probe": "k8s_clinical_pod_status",
            "symptom_group": "workload_resource",
            "alert_hint": "HighCPUUsage cpu 90%",
            "result": "PASSED",
            "extracted_fact": json.dumps({"kind": "PodStatus", "phase": "Running"}),
        },
        "k8s_clinical_pod_metrics": {
            "probe": "k8s_clinical_pod_metrics",
            "symptom_group": "workload_resource",
            "alert_hint": "HighCPUUsage cpu 90%",
            "result": "PASSED",
            "extracted_fact": json.dumps(
                {
                    "containers": [
                        {"name": "nginx", "cpu": "0", "memory": "4528Ki"},
                    ]
                }
            ),
            "raw": "nginx: cpu=0 memory=4528Ki",
        },
    }
    out = compare_alert_claim_to_sdk_state(by_probe)
    assert out is not None
    assert "Alert claims elevated workload CPU" in out
    assert "inconsistent with live cluster state" in out


def test_no_contrast_when_sdk_cpu_nonzero() -> None:
    by_probe = {
        "k8s_clinical_pod_metrics": {
            "probe": "k8s_clinical_pod_metrics",
            "symptom_group": "workload_resource",
            "alert_hint": "HighCPUUsage",
            "result": "PASSED",
            "extracted_fact": json.dumps(
                {"containers": [{"name": "x", "cpu": "100m", "memory": "10Mi"}]}
            ),
        },
    }
    assert compare_alert_claim_to_sdk_state(by_probe) is None


def test_no_contrast_when_sdk_state_invalidates_comparison() -> None:
    """Pending + CreateContainerError — không so “CPU nóng vs ~0” có nghĩa."""
    by_probe = {
        "k8s_clinical_pod_status": {
            "probe": "k8s_clinical_pod_status",
            "symptom_group": "workload_resource",
            "alert_hint": "HighCPUUsage",
            "result": "PASSED",
            "extracted_fact": json.dumps(
                {
                    "phase": "Pending",
                    "container_signals": ["nginx:waiting=CreateContainerError"],
                }
            ),
        },
        "k8s_clinical_pod_metrics": {
            "probe": "k8s_clinical_pod_metrics",
            "symptom_group": "workload_resource",
            "alert_hint": "HighCPUUsage",
            "result": "INCONCLUSIVE",
            "extracted_fact": json.dumps({"omit_reason": "podmetrics_not_found_404"}),
        },
    }
    assert compare_alert_claim_to_sdk_state(by_probe) is None


def test_no_contrast_when_metrics_inconclusive_404() -> None:
    by_probe = {
        "k8s_clinical_pod_status": {
            "probe": "k8s_clinical_pod_status",
            "symptom_group": "workload_resource",
            "alert_hint": "HighCPUUsage cpu 90%",
            "result": "PASSED",
            "extracted_fact": json.dumps({"kind": "PodStatus", "phase": "Running"}),
        },
        "k8s_clinical_pod_metrics": {
            "probe": "k8s_clinical_pod_metrics",
            "symptom_group": "workload_resource",
            "alert_hint": "HighCPUUsage cpu 90%",
            "result": "INCONCLUSIVE",
            "extracted_fact": json.dumps({"omit_reason": "podmetrics_not_found_404"}),
        },
    }
    assert compare_alert_claim_to_sdk_state(by_probe) is None


def test_no_contrast_when_labels_reason_kube_state() -> None:
    canon = json.dumps(
        {
            "labels": {
                "pod": "p",
                "namespace": "ns",
                "reason": "CreateContainerError",
            },
            "annotations": {},
        }
    )
    by_probe = {
        "k8s_clinical_pod_status": {
            "probe": "k8s_clinical_pod_status",
            "symptom_group": "workload_resource",
            "canonical_query_snippet": canon,
            "result": "PASSED",
            "extracted_fact": json.dumps({"phase": "Running"}),
        },
        "k8s_clinical_pod_metrics": {
            "probe": "k8s_clinical_pod_metrics",
            "symptom_group": "workload_resource",
            "canonical_query_snippet": canon,
            "result": "PASSED",
            "extracted_fact": json.dumps(
                {"containers": [{"name": "c", "cpu": "0", "memory": "1Mi"}]}
            ),
        },
    }
    assert compare_alert_claim_to_sdk_state(by_probe) is None


def test_contrast_with_canonical_labels_pod_ns_no_reason() -> None:
    canon = json.dumps(
        {"labels": {"pod": "p", "namespace": "ns"}, "annotations": {}}
    )
    by_probe = {
        "k8s_clinical_pod_status": {
            "probe": "k8s_clinical_pod_status",
            "symptom_group": "workload_resource",
            "canonical_query_snippet": canon,
            "result": "PASSED",
            "extracted_fact": json.dumps({"phase": "Running"}),
        },
        "k8s_clinical_pod_metrics": {
            "probe": "k8s_clinical_pod_metrics",
            "symptom_group": "workload_resource",
            "canonical_query_snippet": canon,
            "result": "PASSED",
            "extracted_fact": json.dumps(
                {"containers": [{"name": "c", "cpu": "0", "memory": "4528Ki"}]}
            ),
        },
    }
    out = compare_alert_claim_to_sdk_state(by_probe)
    assert out is not None and "inconsistent" in out


def test_no_contrast_without_workload_symptom_group() -> None:
    by_probe = {
        "k8s_clinical_pod_metrics": {
            "probe": "k8s_clinical_pod_metrics",
            "alert_hint": "HighCPUUsage cpu 90%",
            "result": "PASSED",
            "extracted_fact": json.dumps(
                {
                    "containers": [
                        {"name": "nginx", "cpu": "0", "memory": "4528Ki"},
                    ]
                }
            ),
        },
    }
    assert compare_alert_claim_to_sdk_state(by_probe) is None
