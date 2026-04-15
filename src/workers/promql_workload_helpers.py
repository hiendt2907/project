"""Workload prefix for cAdvisor PromQL — shared with diagnostic_resource logic, no AnomalyEvent import."""

from __future__ import annotations

import re
from typing import Any

# ReplicaSet-shaped pod (Deployment): <name>-<rs-hash>-<suffix>
_DEPLOYMENT_MANAGED_POD = re.compile(
    r"^(.+)-[a-f0-9]{8,10}-[a-z0-9]{3,8}$",
    re.IGNORECASE,
)


def workload_prefix_from_tool_args(args: dict[str, Any]) -> str | None:
    """
    Prefer explicit deployment/workload labels; else derive prefix from ReplicaSet pod or StatefulSet ordinal.
    Mirrors diagnostic_resource.workload_pod_prefix_for_promql without AnomalyEvent.
    """
    for key in ("deployment", "deployment_name", "workload", "statefulset"):
        raw = args.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    pod_raw = args.get("pod_name") if args.get("pod_name") is not None else args.get("pod")
    if pod_raw is None:
        return None
    pn = str(pod_raw).strip()
    if not pn:
        return None
    m = _DEPLOYMENT_MANAGED_POD.match(pn)
    if m:
        return m.group(1)
    mo = re.match(r"^(.+)-(\d+)$", pn)
    if mo:
        return mo.group(1)
    return None


def workload_pod_label_for_cadvisor(workload_prefix: str) -> str:
    """Label fragment: pod=~\"^<prefix>-.*\" (escaped for PromQL)."""
    w = (workload_prefix or "").strip()
    if not w:
        raise ValueError("workload_prefix rỗng")
    esc = re.escape(w).replace(r"\-", "-")
    return f'pod=~"^{esc}-.*"'
