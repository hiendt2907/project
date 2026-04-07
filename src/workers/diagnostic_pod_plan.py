"""Tier-2 smart probe plan from Tier-1 k8s_clinical_pod_status structured_hint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Mode = Literal["pod_state", "workload_resource"]


@dataclass(frozen=True)
class PodStatusSnapshot:
    phase: str
    ready_false: bool
    waiting_reasons: tuple[str, ...]
    has_crash_loop: bool
    has_oom_killed: bool = False
    namespace: str = ""
    pod: str = ""

    @property
    def phase_norm(self) -> str:
        return (self.phase or "").strip()


def snapshot_from_structured_hint(d: dict[str, Any]) -> PodStatusSnapshot:
    """Build snapshot from k8s_clinical_pod_status ProbeRunRaw.structured_hint / extracted_fact."""
    phase = str(d.get("phase") or "").strip() or "?"
    ready_false = bool(d.get("ready_false"))
    wr = d.get("waiting_reasons")
    if isinstance(wr, list):
        waiting_reasons = tuple(str(x).strip() for x in wr if str(x).strip())
    else:
        waiting_reasons = ()
    has_crash_loop = bool(d.get("has_crash_loop")) or any(
        x == "CrashLoopBackOff" for x in waiting_reasons
    )
    has_oom_killed = bool(d.get("has_oom_killed"))
    ns = str(d.get("namespace") or "").strip()
    pod = str(d.get("pod") or "").strip()
    return PodStatusSnapshot(
        phase=phase,
        ready_false=ready_false,
        waiting_reasons=waiting_reasons,
        has_crash_loop=has_crash_loop,
        has_oom_killed=has_oom_killed,
        namespace=ns,
        pod=pod,
    )


def _has_waiting_reason(snapshot: PodStatusSnapshot, *reasons: str) -> bool:
    s = set(snapshot.waiting_reasons)
    return any(r in s for r in reasons)


def get_smart_diagnostic_plan(snapshot: PodStatusSnapshot, *, mode: Mode = "pod_state") -> list[str]:
    """
    Return ordered probe ids after Tier-1 pod status (excludes Tier-1 itself).

    Rules:
    - Succeeded/Completed: no follow-up (avoid GIGO CPU diagnosis on finished pods).
    - CrashLoopBackOff: previous container log + events (not current-only tail).
    - Pending or CreateContainerError/ImagePullBackOff: events + resource quota; no metrics/log tail.
    - Running + Ready=False: log tail + events.
    - Otherwise (Running stable, etc.): metrics + log + Prom (pod_state) or workload_resource variant.
    """
    ph = snapshot.phase_norm

    if ph in ("Succeeded", "Completed"):
        return []

    if snapshot.has_crash_loop or _has_waiting_reason(snapshot, "CrashLoopBackOff"):
        return ["k8s_clinical_pod_log_previous", "k8s_clinical_pod_events"]

    pending_or_image = ph == "Pending" or _has_waiting_reason(
        snapshot,
        "CreateContainerError",
        "CreateContainerConfigError",
        "ImagePullBackOff",
        "ErrImagePull",
    )
    if pending_or_image:
        return ["k8s_clinical_pod_events", "k8s_resource_quota_probe"]

    if snapshot.has_oom_killed and not snapshot.has_crash_loop:
        return [
            "k8s_clinical_pod_metrics",
            "prom_pod_memory_wss",
        ]

    if ph == "Running" and snapshot.ready_false:
        return ["k8s_clinical_pod_log_tail", "k8s_clinical_pod_events"]

    # Default: full workload-style follow-up
    if mode == "workload_resource":
        return [
            "k8s_clinical_pod_metrics",
            "k8s_clinical_pod_log_tail",
            "prom_pod_cpu_cores",
            "prom_pod_memory_wss",
        ]
    return [
        "k8s_clinical_pod_metrics",
        "k8s_clinical_pod_log_tail",
        "prom_pod_cpu_cores",
        "prom_pod_memory_wss",
    ]
