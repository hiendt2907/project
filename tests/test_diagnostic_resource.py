"""Resource alert → Prom probes plan (no redis)."""

from __future__ import annotations

import json

from workers.diagnostic_resource import (
    is_kube_pod_container_state_alert,
    is_workload_resource_alert,
    kube_pod_state_probe_ids,
    pod_identity_from_event,
    resource_probe_ids,
)
from workers.proactive_models import AnomalyEvent


def test_resource_alert_with_pod_labels() -> None:
    cq = json.dumps(
        {
            "labels": {
                "alertname": "HighCPU",
                "namespace": "multi-agent",
                "pod": "nginx-test-xxx",
            },
            "annotations": {"summary": "cpu 90%"},
        }
    )
    ev = AnomalyEvent(
        trace_id="t-1234",
        canonical_query=cq,
        error_hint="HighCPU cpu 90%",
        namespace="multi-agent",
    )
    assert is_workload_resource_alert(ev) is True
    ids = resource_probe_ids()
    assert ids[:3] == [
        "k8s_clinical_pod_status",
        "k8s_clinical_pod_metrics",
        "k8s_clinical_pod_log_tail",
    ]
    assert ids[-2:] == ["prom_pod_cpu_cores", "prom_pod_memory_wss"]


def test_non_resource_no_pod() -> None:
    ev = AnomalyEvent(
        trace_id="t-1234",
        canonical_query="{}",
        error_hint="cpu high",
        namespace="",
    )
    assert is_workload_resource_alert(ev) is False


def test_kube_pod_container_state_alert_with_reason() -> None:
    cq = json.dumps(
        {
            "labels": {
                "alertname": "NginxTestContainerWaitingFaultLab",
                "namespace": "multi-agent",
                "pod": "nginx-test-abc",
                "reason": "CreateContainerError",
            },
            "annotations": {},
        }
    )
    ev = AnomalyEvent(
        trace_id="t-state",
        canonical_query=cq,
        error_hint="waiting fault",
        namespace="multi-agent",
    )
    assert is_kube_pod_container_state_alert(ev) is True
    assert is_workload_resource_alert(ev) is False
    assert kube_pod_state_probe_ids()[:3] == [
        "k8s_clinical_pod_status",
        "k8s_clinical_pod_metrics",
        "k8s_clinical_pod_log_tail",
    ]


def test_kube_state_metric_not_workload_resource() -> None:
    cq = json.dumps(
        {
            "labels": {
                "alertname": "PodWaiting",
                "namespace": "ns",
                "pod": "p",
                "__name__": "kube_pod_container_status_waiting_reason",
                "reason": "CreateContainerError",
            },
            "annotations": {},
        }
    )
    ev = AnomalyEvent(
        trace_id="t-kube",
        canonical_query=cq,
        error_hint="PodWaiting memory check",
        namespace="ns",
    )
    assert is_workload_resource_alert(ev) is False


def test_labels_reason_excludes_workload_resource() -> None:
    cq = json.dumps(
        {
            "labels": {
                "alertname": "Whatever",
                "namespace": "ns",
                "pod": "p",
                "reason": "CrashLoopBackOff",
            },
            "annotations": {},
        }
    )
    ev = AnomalyEvent(
        trace_id="t-reason",
        canonical_query=cq,
        error_hint="cpu high",
        namespace="ns",
    )
    assert is_workload_resource_alert(ev) is False


def test_resource_alert_pod_from_description_when_label_missing() -> None:
    """Nhiều rule chỉ mô tả 'in pod X' trong annotations, không set label pod."""
    cq = json.dumps(
        {
            "labels": {
                "alertname": "HighCPUUsage",
                "namespace": "multi-agent",
            },
            "annotations": {
                "description": "Container nginx in pod nginx-test-abc123 CPU ~90%",
            },
        }
    )
    ev = AnomalyEvent(
        trace_id="t-desc",
        canonical_query=cq,
        error_hint="HighCPUUsage CPU 90%",
        namespace="multi-agent",
    )
    assert is_workload_resource_alert(ev) is True
    ns, pod, _ = pod_identity_from_event(ev)
    assert ns == "multi-agent"
    assert pod == "nginx-test-abc123"
