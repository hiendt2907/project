"""Smart diagnostic plan from PodStatusSnapshot."""

from __future__ import annotations

from workers.diagnostic_pod_plan import (
    PodStatusSnapshot,
    get_smart_diagnostic_plan,
    snapshot_from_structured_hint,
)


def test_pending_create_container_no_log_metrics() -> None:
    snap = PodStatusSnapshot(
        phase="Pending",
        ready_false=True,
        waiting_reasons=("CreateContainerError",),
        has_crash_loop=False,
    )
    plan = get_smart_diagnostic_plan(snap, mode="pod_state")
    assert "k8s_clinical_pod_log_tail" not in plan
    assert "k8s_clinical_pod_metrics" not in plan
    assert plan == ["k8s_clinical_pod_events", "k8s_resource_quota_probe"]


def test_pending_create_container_config_error_same_as_create() -> None:
    snap = PodStatusSnapshot(
        phase="Pending",
        ready_false=True,
        waiting_reasons=("CreateContainerConfigError",),
        has_crash_loop=False,
    )
    plan = get_smart_diagnostic_plan(snap, mode="pod_state")
    assert plan == ["k8s_clinical_pod_events", "k8s_resource_quota_probe"]


def test_crash_loop_previous_and_events() -> None:
    snap = PodStatusSnapshot(
        phase="Running",
        ready_false=True,
        waiting_reasons=("CrashLoopBackOff",),
        has_crash_loop=True,
    )
    plan = get_smart_diagnostic_plan(snap, mode="pod_state")
    assert "k8s_clinical_pod_log_previous" in plan
    assert "k8s_clinical_pod_events" in plan
    assert "k8s_clinical_pod_log_tail" not in plan


def test_running_ready_false_log_and_events() -> None:
    snap = PodStatusSnapshot(
        phase="Running",
        ready_false=True,
        waiting_reasons=(),
        has_crash_loop=False,
    )
    plan = get_smart_diagnostic_plan(snap, mode="pod_state")
    assert plan == ["k8s_clinical_pod_log_tail", "k8s_clinical_pod_events"]


def test_succeeded_no_followup() -> None:
    snap = PodStatusSnapshot(
        phase="Succeeded",
        ready_false=False,
        waiting_reasons=(),
        has_crash_loop=False,
    )
    assert get_smart_diagnostic_plan(snap, mode="pod_state") == []


def test_snapshot_from_structured_hint() -> None:
    d = {
        "kind": "PodStatus",
        "phase": "Running",
        "ready_false": True,
        "waiting_reasons": ["CrashLoopBackOff"],
        "has_crash_loop": True,
        "namespace": "ns",
        "pod": "p",
    }
    s = snapshot_from_structured_hint(d)
    assert s.phase == "Running"
    assert s.has_crash_loop is True
    assert "CrashLoopBackOff" in s.waiting_reasons


def test_workload_resource_default_includes_prom() -> None:
    snap = PodStatusSnapshot(
        phase="Running",
        ready_false=False,
        waiting_reasons=(),
        has_crash_loop=False,
    )
    plan = get_smart_diagnostic_plan(snap, mode="workload_resource")
    assert "prom_pod_cpu_cores" in plan
    assert "k8s_clinical_pod_metrics" in plan


def test_oom_killed_metrics_and_memory_prom_only() -> None:
    snap = PodStatusSnapshot(
        phase="Running",
        ready_false=True,
        waiting_reasons=(),
        has_crash_loop=False,
        has_oom_killed=True,
    )
    plan = get_smart_diagnostic_plan(snap, mode="pod_state")
    assert plan == [
        "k8s_clinical_pod_metrics",
        "prom_pod_memory_wss",
    ]
