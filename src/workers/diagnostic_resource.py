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


def is_workload_resource_alert(ev: AnomalyEvent) -> bool:
    """True → dispatcher chạy probe Prom pod CPU/mem, cấm redis_ping/kafka generic."""
    if not _RESOURCE_HINT.search(ev.error_hint or ""):
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
