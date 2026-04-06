"""SDK-first clinical inspection for omni-prober (logic lives in ``workers.diagnostic_k8s_clinical``)."""

from __future__ import annotations

from workers.diagnostic_k8s_clinical import (
    probe_k8s_clinical_pod_log_tail,
    probe_k8s_clinical_pod_metrics,
    probe_k8s_clinical_pod_status,
)

__all__ = [
    "probe_k8s_clinical_pod_log_tail",
    "probe_k8s_clinical_pod_metrics",
    "probe_k8s_clinical_pod_status",
]
