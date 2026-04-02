"""PromQL động — host (node_exporter), pod (cAdvisor), kube-state (kube_*).

Pod usage: namespace + pod (cAdvisor). Cluster state: kube-state-metrics.
"""

from __future__ import annotations

import re
from typing import TypedDict


class PromqlBuildMeta(TypedDict, total=False):
    used_profile: str
    fallback_note: str


def _norm_intent(intent: str) -> str:
    raw = (intent or "cpu").strip().lower()
    aliases = {
        "memory": "ram",
        "mem": "ram",
        "container_cpu": "cpu",
        "cpu_usage": "cpu",
        "filesystem": "disk",
        "io": "disk_io",
        "net": "network",
    }
    return aliases.get(raw, raw)


def _esc_label(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def build_dynamic_promql(
    target_type: str,
    metric: str,
    *,
    pod_name: str | None = None,
    namespace: str | None = None,
    node: str | None = None,
) -> tuple[str, str, PromqlBuildMeta]:
    """
    Sinh PromQL — host dùng node_*; pod bắt buộc cả namespace + pod (label kube cAdvisor).

    Trả về (query, human_note, meta).
    """
    tt = (target_type or "pod").strip().lower()
    key = _norm_intent(metric)
    meta: PromqlBuildMeta = {}

    if tt == "host":
        # Ưu tiên node_exporter (host: node_*)
        if key == "cpu":
            q = 'sum(rate(node_cpu_seconds_total{mode!="idle"}[5m]))'
            meta["used_profile"] = "node_exporter_cpu_non_idle_rate"
            fb = (
                'avg(1 - rate(node_cpu_seconds_total{mode="idle"}[5m])) '
                "* scalar(count(node_cpu_seconds_total))"
            )
            meta["fallback_note"] = f"fallback nếu series rỗng: {fb}"
            return q, "host/node_exporter CPU (non-idle rate sum)", meta
        if key in ("ram", "memory"):
            q = "node_memory_MemAvailable_bytes"
            meta["used_profile"] = "node_exporter_mem_available"
            meta["fallback_note"] = "fallback: node_memory_MemTotal_bytes - node_memory_MemFree_bytes"
            return q, "host/node_exporter RAM (MemAvailable bytes)", meta
        if key == "disk":
            q = "sum(node_filesystem_avail_bytes)"
            meta["used_profile"] = "node_exporter_fs_avail"
            return q, "host/node_exporter disk (fs avail sum)", meta
        if key == "disk_io":
            if node and str(node).strip():
                n = re.escape(str(node).strip())
                q = f'sum(rate(node_disk_read_bytes_total{{instance=~".*{n}.*"}}[5m]))'
            else:
                q = "sum(rate(node_disk_read_bytes_total[5m]))"
            meta["used_profile"] = "node_exporter_disk_read"
            return q, "host/node_exporter disk read throughput", meta
        if key == "network":
            q = "sum(rate(node_network_receive_bytes_total[5m]))"
            meta["used_profile"] = "node_exporter_net_in"
            return q, "host/node_exporter network receive rate", meta
        q = 'sum(rate(node_cpu_seconds_total{mode!="idle"}[5m]))'
        meta["used_profile"] = "node_exporter_cpu_default"
        return q, "host/node_exporter CPU (default)", meta

    # --- pod (cAdvisor / kubelet) — BẮT BUỘC namespace + pod ---
    if not namespace or not str(namespace).strip():
        raise ValueError("pod PromQL: thiếu namespace")
    if not pod_name or not str(pod_name).strip():
        raise ValueError("pod PromQL: thiếu pod_name")
    ns_esc = _esc_label(str(namespace).strip())
    pn = str(pod_name).strip()
    # Full pod name (hash-suffix) → khớp chính xác; gợi ý workload ngắn → prefix regex
    _looks_full = bool(re.search(r"-[a-z0-9]{8,12}-[a-z0-9]{3,8}$", pn, re.I))
    if _looks_full:
        pod_label = f'pod="{_esc_label(pn)}"'
    else:
        pod_re = re.escape(pn).replace(r"\-", "-")
        pod_label = f'pod=~"^{pod_re}.*"'

    if key == "cpu":
        q = f'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns_esc}",{pod_label}}}[5m]))'
    elif key in ("ram", "memory"):
        q = f'sum(container_memory_working_set_bytes{{namespace="{ns_esc}",{pod_label}}})'
    elif key == "disk":
        q = f'sum(container_fs_usage_bytes{{namespace="{ns_esc}",{pod_label}}})'
    elif key == "disk_io":
        q = f'sum(rate(container_fs_reads_total{{namespace="{ns_esc}",{pod_label}}}[5m]))'
    elif key == "network":
        q = f'sum(rate(container_network_receive_bytes_total{{namespace="{ns_esc}",{pod_label}}}[5m]))'
    else:
        q = f'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns_esc}",{pod_label}}}[5m]))'
    meta["used_profile"] = "cAdvisor_pod_strict_ns_pod"
    return q, f'pod/cAdvisor namespace="{ns_esc}" {pod_label}', meta


def build_promql_from_intent(
    intent: str,
    *,
    namespace: str | None = None,
    pod_name: str | None = None,
    node: str | None = None,
) -> str:
    """Tương thích test/cũ — luôn sinh PromQL pod (cần đủ namespace + pod)."""
    q, _note, _meta = build_dynamic_promql(
        "pod",
        intent,
        pod_name=pod_name,
        namespace=namespace,
        node=node,
    )
    return q


def _norm_kube_intent(intent: str) -> str:
    raw = (intent or "replica_ratio").strip().lower()
    aliases = {
        "replicas": "replica_ratio",
        "replica": "replica_ratio",
        "deployment": "replica_ratio",
        "pods": "pods_running",
        "running": "pods_running",
        "pending": "pods_pending",
        "pod_running": "pods_running",
        "pod_pending": "pods_pending",
        "replicas_available": "replicas_available",
        "replicas_desired": "replicas_desired",
        "spec_replicas": "replicas_desired",
    }
    return aliases.get(raw, raw)


def build_kube_state_promql(
    intent: str,
    *,
    namespace: str,
    deployment: str | None = None,
) -> tuple[str, str, PromqlBuildMeta]:
    """
    kube-state-metrics (cần scrape job `kube-state-metrics` trong Prometheus).

    - ``deployment`` + ``namespace``: replica available/spec ratio hoặc intent cụ thể.
    - Chỉ ``namespace``: đếm pod Running / Pending.
    """
    if not namespace or not str(namespace).strip():
        raise ValueError("kube-state PromQL: thiếu namespace")
    ns_esc = _esc_label(str(namespace).strip())
    key = _norm_kube_intent(intent)
    meta: PromqlBuildMeta = {}

    if deployment and str(deployment).strip():
        dep_esc = _esc_label(str(deployment).strip())
        if key == "replica_ratio":
            q = (
                f'kube_deployment_status_replicas_available{{namespace="{ns_esc}",deployment="{dep_esc}"}} '
                f'/ kube_deployment_spec_replicas{{namespace="{ns_esc}",deployment="{dep_esc}"}}'
            )
            meta["used_profile"] = "kube_state_deployment_replica_ratio"
            return (
                q,
                f'kube-state ratio available/spec deployment="{dep_esc}" ns="{ns_esc}"',
                meta,
            )
        if key == "replicas_available":
            q = f'kube_deployment_status_replicas_available{{namespace="{ns_esc}",deployment="{dep_esc}"}}'
            meta["used_profile"] = "kube_state_replicas_available"
            return q, f'kube-state replicas_available deployment="{dep_esc}"', meta
        if key == "replicas_desired":
            q = f'kube_deployment_spec_replicas{{namespace="{ns_esc}",deployment="{dep_esc}"}}'
            meta["used_profile"] = "kube_state_spec_replicas"
            return q, f'kube-state spec_replicas deployment="{dep_esc}"', meta
        q = (
            f'kube_deployment_status_replicas_available{{namespace="{ns_esc}",deployment="{dep_esc}"}} '
            f'/ kube_deployment_spec_replicas{{namespace="{ns_esc}",deployment="{dep_esc}"}}'
        )
        meta["used_profile"] = "kube_state_deployment_replica_ratio_default"
        return q, f'kube-state replica ratio (default) deployment="{dep_esc}"', meta

    if key == "replica_ratio" or key in ("replicas_available", "replicas_desired"):
        raise ValueError("intent replica/replicas cần thêm deployment (kube_deployment)")

    if key == "pods_running":
        q = f'sum(kube_pod_status_phase{{namespace="{ns_esc}",phase="Running"}})'
        meta["used_profile"] = "kube_state_pods_running"
        return q, f'kube-state sum Running pods ns="{ns_esc}"', meta
    if key == "pods_pending":
        q = f'sum(kube_pod_status_phase{{namespace="{ns_esc}",phase="Pending"}})'
        meta["used_profile"] = "kube_state_pods_pending"
        return q, f'kube-state sum Pending pods ns="{ns_esc}"', meta

    q = f'sum(kube_pod_status_phase{{namespace="{ns_esc}",phase="Running"}})'
    meta["used_profile"] = "kube_state_pods_running_default"
    return q, f'kube-state Running pods (default) ns="{ns_esc}"', meta


def resolve_intent_from_keywords(text: str) -> str:
    """Gợi ý intent từ câu tiếng Việt/Anh (CPU/RAM/đĩa/mạng)."""
    t = (text or "").lower()
    if any(k in t for k in ("ram", "memory", "bộ nhớ", "mem ")):
        return "ram"
    if any(k in t for k in ("disk", "đĩa", "ổ", "fs ", "filesystem")):
        return "disk"
    if any(k in t for k in ("network", "mạng", "traffic", "băng thông")):
        return "network"
    if any(k in t for k in ("iops", "disk io", "read bytes", "ghi đĩa")):
        return "disk_io"
    if any(k in t for k in ("cpu", "vcpu", "cores")):
        return "cpu"
    return "cpu"
