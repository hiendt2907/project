"""Activation matrix for compare_alert_claim_to_sdk_state (STATE_MACHINE_CONTRAST gate)."""

from __future__ import annotations

import json

import pytest

from workers.alert_sdk_truth_compare import compare_alert_claim_to_sdk_state


def _labels_snip(**labels: str) -> str:
    return json.dumps({"labels": dict(labels), "annotations": {}})


def _base_workload_batch(**overrides: dict) -> dict[str, dict]:
    """Minimal passing-shaped batch; override keys to break activation."""
    base = {
        "k8s_clinical_pod_metrics": {
            "symptom_group": "workload_resource",
            "canonical_query_snippet": _labels_snip(
                alertname="HighCPUUsage",
                namespace="multi-agent",
                pod="nginx-test-abc",
                deployment="nginx-test",
                container="nginx",
                severity="warning",
            ),
            "result": "PASSED",
            "extracted_fact": json.dumps(
                {"containers": [{"name": "nginx", "cpu": "0", "memory": "5768Ki"}]}
            ),
            "alert_hint": "HighCPUUsage namespace=multi-agent pod=nginx-test-abc",
        },
        "k8s_clinical_pod_status": {
            "symptom_group": "workload_resource",
            "extracted_fact": json.dumps(
                {
                    "pods": [
                        {
                            "pod": "nginx-test-abc",
                            "namespace": "multi-agent",
                            "phase": "Running",
                        }
                    ]
                }
            ),
        },
    }
    out = {**base, **overrides}
    return out


def test_contrast_active_when_all_gates_pass() -> None:
    r = compare_alert_claim_to_sdk_state(_base_workload_batch())
    assert r is not None
    assert "Tin vào state machine" in r
    assert "đáng nghi" in r.lower()


@pytest.mark.parametrize(
    "mutator,why",
    [
        (
            lambda b: {k: v for k, v in b.items() if k != "k8s_clinical_pod_metrics"},
            "missing pod metrics probe",
        ),
        (
            lambda b: {
                **b,
                "k8s_clinical_pod_metrics": {
                    **b["k8s_clinical_pod_metrics"],
                    "result": "FAILED",
                },
            },
            "metrics probe not PASSED/INCONCLUSIVE",
        ),
        (
            lambda b: {
                **b,
                "k8s_clinical_pod_metrics": {
                    **b["k8s_clinical_pod_metrics"],
                    "extracted_fact": json.dumps({"containers": [{"name": "nginx", "cpu": "100m", "memory": "1Mi"}]}),
                },
            },
            "cpu not effectively zero",
        ),
        (
            lambda b: {
                **b,
                "k8s_clinical_pod_status": {
                    "symptom_group": "workload_resource",
                    "extracted_fact": json.dumps(
                        {
                            "phase": "Pending",
                            "pods": [{"pod": "nginx-test-abc", "namespace": "multi-agent", "phase": "Pending"}],
                        }
                    ),
                },
            },
            "top-level pod phase Pending invalidates cpu vs hot-alert comparison",
        ),
        (
            lambda b: {
                **b,
                "k8s_clinical_pod_metrics": {
                    **b["k8s_clinical_pod_metrics"],
                    "canonical_query_snippet": _labels_snip(
                        alertname="HighCPUUsage",
                        namespace="multi-agent",
                        pod="nginx-test-abc",
                        reason="NodeShutdown",
                    ),
                    "result": "PASSED",
                },
            },
            "labels.reason present",
        ),
        (
            lambda b: {
                "other": {"symptom_group": "network", "result": "PASSED"},
            },
            "no workload_resource in batch",
        ),
    ],
)
def test_contrast_inactive(mutator, why: str) -> None:
    base = _base_workload_batch()
    batch = mutator(base)
    assert compare_alert_claim_to_sdk_state(batch) is None, why


def test_inconclusive_404_returns_none() -> None:
    batch = _base_workload_batch(
        k8s_clinical_pod_metrics={
            "symptom_group": "workload_resource",
            "canonical_query_snippet": _labels_snip(
                alertname="HighCPUUsage",
                namespace="multi-agent",
                pod="nginx-test-abc",
            ),
            "result": "INCONCLUSIVE",
            "extracted_fact": json.dumps({"omit_reason": "404 podmetrics not_found", "containers": []}),
        }
    )
    assert compare_alert_claim_to_sdk_state(batch) is None


def test_inconclusive_with_zero_cpu_containers_still_activates() -> None:
    batch = _base_workload_batch(
        k8s_clinical_pod_metrics={
            "symptom_group": "workload_resource",
            "canonical_query_snippet": _labels_snip(
                alertname="HighCPUUsage",
                namespace="multi-agent",
                pod="nginx-test-abc",
            ),
            "result": "INCONCLUSIVE",
            "extracted_fact": json.dumps(
                {"omit_reason": "rate_limited", "containers": [{"name": "nginx", "cpu": "0", "memory": "1Mi"}]}
            ),
            "alert_hint": "cpu hot",
        }
    )
    r = compare_alert_claim_to_sdk_state(batch)
    assert r is not None
    assert "Tin vào state machine" in r


def _mem_alert_batch(mem: str) -> dict[str, dict]:
    """Memory-dimension alert batch (PodMemoryWorkingSetVsLimitHigh), CPU idle."""
    return {
        "k8s_clinical_pod_metrics": {
            "symptom_group": "workload_resource",
            "canonical_query_snippet": _labels_snip(
                alertname="PodMemoryWorkingSetVsLimitHigh",
                namespace="multi-agent",
                pod="nginx-test-abc",
                deployment="nginx-test",
                container="nginx",
                severity="warning",
            ),
            "result": "PASSED",
            "extracted_fact": json.dumps(
                {"containers": [{"name": "nginx", "cpu": "0", "memory": mem}]}
            ),
            "alert_hint": "PodMemoryWorkingSetVsLimitHigh memory namespace=multi-agent pod=nginx-test-abc",
        },
        "k8s_clinical_pod_status": {
            "symptom_group": "workload_resource",
            "extracted_fact": json.dumps(
                {"pods": [{"pod": "nginx-test-abc", "namespace": "multi-agent", "phase": "Running"}]}
            ),
        },
    }


def test_memory_alert_high_memory_not_suppressed() -> None:
    """Real memory pressure (high mem) + idle CPU must NOT be dismissed as false alarm."""
    r = compare_alert_claim_to_sdk_state(_mem_alert_batch("1900Mi"))
    assert r is None


def test_memory_alert_low_memory_suppressed_with_memory_wording() -> None:
    """Memory alert contradicted by low live memory -> contrast worded for memory, not CPU."""
    r = compare_alert_claim_to_sdk_state(_mem_alert_batch("5768Ki"))
    assert r is not None
    assert "bộ nhớ" in r.lower()
    assert "alert báo bộ nhớ workload cao" in r.lower()
    assert "alert báo cpu workload cao" not in r.lower()
