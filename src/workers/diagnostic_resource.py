"""Phân loại alert workload CPU/mem → probe PromQL pod, không dùng redis/kafka generic."""

from __future__ import annotations

import json
import re
from typing import Any

from workers.proactive_models import AnomalyEvent

_RESOURCE_HINT = re.compile(
    r"(highcpu|\bcpu\b|memory|\boom\b|oom|throttl|cgroup|millicore|millicores|rss|usage|\d+\s*%)",
    re.IGNORECASE,
)

# Alertmanager hay mô tả "Container X in pod <name>" nhưng rule không gắn label pod.
_POD_FROM_TEXT = re.compile(
    r"(?:\bin pod\s+|\bpod/\s*)([\w.-]+)",
    re.IGNORECASE,
)


def _pod_name_from_free_text(*parts: str) -> str:
    blob = " ".join(p for p in parts if p)
    m = _POD_FROM_TEXT.search(blob)
    return m.group(1).strip() if m else ""


def _labels_from_canonical(ev: AnomalyEvent) -> dict[str, Any]:
    cq = (ev.canonical_query or "").strip()
    if not cq.startswith("{"):
        return {}
    try:
        o = json.loads(cq)
        L = o.get("labels")
        return dict(L) if isinstance(L, dict) else {}
    except Exception:
        return {}


def canonical_flat_labels(ev: AnomalyEvent) -> dict[str, str]:
    """String labels from canonical_query JSON (alert / kube-state / Prom). Used when the alert pod is already gone."""
    out: dict[str, str] = {}
    for k, v in _labels_from_canonical(ev).items():
        if v is None:
            continue
        s = str(v).strip()
        if s:
            out[str(k)] = s
    return out


def _is_kube_state_pod_metric(labels: dict[str, Any]) -> bool:
    """Alert kube-state-metrics (pod/container state) — không phải usage rate cAdvisor."""
    name = str(labels.get("__name__") or "")
    if not name:
        return False
    if "kube_pod_container_status" in name or "kube_pod_status" in name:
        return True
    if "kube_pod_container_state" in name:
        return True
    return False


def deployment_workload_from_event(ev: AnomalyEvent) -> tuple[str, str]:
    """
    namespace + stable workload name for gates and LLM — prefer Deployment identity.

    Does **not** derive deployment from pod name; use labels (deployment, workload, …) only.
    """
    labels = canonical_flat_labels(ev)
    ns = (ev.namespace or "").strip() or str(labels.get("namespace") or "").strip()
    dep = (ev.deployment or "").strip()
    if not dep:
        dep = str(labels.get("deployment") or labels.get("deployment_name") or "").strip()
    if not dep:
        dep = str(labels.get("workload") or "").strip()
    if not dep:
        dep = str(labels.get("statefulset") or "").strip()
    if not dep and isinstance(ev.gigo_metadata, dict):
        dep = str(
            ev.gigo_metadata.get("deployment")
            or ev.gigo_metadata.get("statefulset")
            or ev.gigo_metadata.get("workload")
            or ""
        ).strip()
    return ns, dep


# ReplicaSet-shaped pod (Deployment): <name>-<rs-hash>-<suffix>
_DEPLOYMENT_MANAGED_POD = re.compile(
    r"^(.+)-[a-f0-9]{8,10}-[a-z0-9]{3,8}$",
    re.IGNORECASE,
)


def workload_pod_prefix_for_promql(ev: AnomalyEvent) -> tuple[str, str]:
    """
    Namespace + workload prefix for cAdvisor PromQL — aggregate all pods of the workload,
    never a single ephemeral pod name (no pod=\"…\" exact match on alert pod).

    Order: deployment/workload/statefulset labels → derive from ReplicaSet-shaped pod name
    → StatefulSet ordinal pod name (foo-0).
    """
    ns, dep = deployment_workload_from_event(ev)
    if not ns:
        ns = (ev.namespace or "").strip()
    if dep:
        return ns, dep
    _, pod, _ = pod_identity_from_event(ev)
    if not pod:
        return ns, ""
    m = _DEPLOYMENT_MANAGED_POD.match(pod)
    if m:
        return ns, m.group(1)
    mo = re.match(r"^(.+)-(\d+)$", pod)
    if mo:
        return ns, mo.group(1)
    return ns, ""


def promql_workload_pod_regex_selector(workload_prefix: str) -> str:
    """Label selector fragment: pod=~\"^<workload>-.*\" (escaped)."""
    w = (workload_prefix or "").strip()
    if not w:
        return ""
    esc = re.escape(w).replace(r"\-", "-")
    return f'pod=~"^{esc}-.*"'


def pod_identity_from_event(ev: AnomalyEvent) -> tuple[str, str, str]:
    """namespace, pod, container từ canonical_query JSON + ev.namespace."""
    ns = (ev.namespace or "").strip()
    pod = ""
    container = ""
    annot_blob = ""
    cq = (ev.canonical_query or "").strip()
    if cq.startswith("{"):
        try:
            o: dict[str, Any] = json.loads(cq)
            labels = o.get("labels")
            if isinstance(labels, dict):
                pod = str(labels.get("pod") or labels.get("pod_name") or "").strip()
                container = str(labels.get("container") or "").strip()
                if not ns:
                    ns = str(labels.get("namespace") or "").strip()
            annot = o.get("annotations")
            if isinstance(annot, dict):
                annot_blob = f"{annot.get('description') or ''} {annot.get('summary') or ''}"
        except Exception:
            pass
    if not pod:
        pod = _pod_name_from_free_text(cq, annot_blob, ev.error_hint or "")
    return ns, pod, container


def is_kube_pod_container_state_alert(ev: AnomalyEvent) -> bool:
    """
    Alert kube-state / container state (có **reason** trên labels) + đủ pod/namespace.
    Chẩn đoán bằng SDK PodStatus/PodMetrics — không dùng redis_ping generic.
    """
    labels = _labels_from_canonical(ev)
    if not str(labels.get("reason") or "").strip():
        return False
    ns, pod, _ = pod_identity_from_event(ev)
    return bool(ns and pod)


def kube_pod_state_probe_ids() -> list[str]:
    """SDK trước, Prom pod-scoped sau để đối chiếu với rule (nếu có series)."""
    return [
        "k8s_clinical_pod_status",
        "k8s_clinical_pod_metrics",
        "k8s_clinical_pod_log_tail",
        "prom_pod_cpu_cores",
        "prom_pod_memory_wss",
    ]


def is_workload_resource_alert(ev: AnomalyEvent) -> bool:
    """True → alert kiểu usage/rate (CPU/mem) trên workload; không phải kube-state **reason**/state series."""
    labels = _labels_from_canonical(ev)
    if str(labels.get("reason") or "").strip():
        return False
    if _is_kube_state_pod_metric(labels):
        return False
    hint_blob = f"{ev.error_hint or ''} {labels.get('alertname', '')}"
    if not _RESOURCE_HINT.search(hint_blob):
        return False
    ns, pod, _ = pod_identity_from_event(ev)
    return bool(ns and pod)


def resource_probe_ids() -> list[str]:
    # SDK real-time trước; Prometheus (historical) sau.
    return [
        "k8s_clinical_pod_status",
        "k8s_clinical_pod_metrics",
        "k8s_clinical_pod_log_tail",
        "prom_pod_cpu_cores",
        "prom_pod_memory_wss",
    ]
