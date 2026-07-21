"""Risk-class taxonomy (bảng TĨNH) — SHARED giữa workers và gateway/admin_config.

Đặt ở ``src/pkg/`` (không phải ``workers/``) để gateway dùng được mà KHÔNG vi phạm
bất biến "gateway KHÔNG import workers". ``workers/risk_class.py`` re-export module này.

MASTER_PLAN §2: 4 mức READONLY < LOW < MEDIUM < HIGH. Phân loại TĨNH. Tool thiếu
trong bảng → **HIGH** (fail-closed). Override theo tenant ở omni_admin.risk_class_override
(DB→cache) nhưng KHÔNG được hạ ``dangerous_tools`` xuống dưới HIGH (clamp + reject lúc ghi).
"""

from __future__ import annotations

from typing import Final

READONLY: Final = "READONLY"
LOW: Final = "LOW"
MEDIUM: Final = "MEDIUM"
HIGH: Final = "HIGH"

VALID_RISK_CLASSES: Final = (READONLY, LOW, MEDIUM, HIGH)
_ORDER: Final = {READONLY: 0, LOW: 1, MEDIUM: 2, HIGH: 3}

# Tập dangerous (khớp advisory_mode_kill_switch.dangerous_tools) — KHÔNG bao giờ < HIGH.
DANGEROUS_TOOLS: Final = frozenset({
    "k8s_delete_pod",
    "k8s_delete_deployment",
    "k8s_delete_pvc",
    "k8s_patch_rbac",
    "k8s_patch_secret",
    "k8s_mutate_taint",
})

# Tool readonly (diagnostic/đọc thuần) — từ TOOL_REGISTRY thực tế.
_READONLY_TOOLS: Final = frozenset({
    "audit_observability_stack", "database_replication_lag", "disk_health", "echo",
    "escalate_to_human", "forecast_memory_risk_vm", "forecast_metric_prophet",
    "get_historical_series_dataframe", "haproxy_stats", "inspect_pod_deep",
    "inspect_pod_details", "k8s_check_endpoints", "k8s_expert_search",
    "k8s_get_deployment_state", "k8s_get_events", "k8s_get_logs", "k8s_list_ingress",
    "k8s_list_nodes", "k8s_list_pods", "k8s_list_resources", "k8s_list_services",
    "k8s_list_workload_pods", "k8s_node_conditions", "k8s_tail_logs",
    "k8s_verify_rollout", "list_all_pods_sdk", "list_namespace_pods",
    "metrics_promql_hints", "mysql_health", "namespace_pods_top",
    "net_scapy_interfaces", "nfs_health", "omni_mark_resolved",
    "predict_resource_exhaustion", "promql_instant", "promql_range", "proxysql_stats",
    "query_historical_metrics", "query_prometheus_metrics", "query_victoria_metrics",
    "query_vm_timeseries", "redis_expert_check", "redis_health", "redis_info", "reply",
    "resolve_deployment_identity", "resolve_pod_identity", "system_psutil",
    "system_psutil_diskio", "systemd_service_health", "timeseries_analyze",
    "vendor_knowledge_search", "viz_line_chart", "viz_vm_range_chart",
    "vm_promql_instant", "vm_promql_range",
})

# Bảng TĨNH cho tool MUTATE/control (MASTER_PLAN §2). Readonly resolve qua _READONLY_TOOLS.
_STATIC_MUTATE: Final = {
    "k8s_rollout_restart": LOW,             # idempotent, tự phục hồi
    # VM/AOIP recovery lane counterpart of k8s_rollout_restart — same
    # justification (idempotent, self-healing, smallest reversible action).
    # Phase 2 of the 0-6 roadmap: without this entry, risk_class_of() would
    # fail-closed to HIGH for every VM recovery command (tool missing from
    # the table), forcing HITL at every tier including auto — not wrong,
    # but not the intended parity with the K8s lane either.
    "systemd.restart_unit": LOW,
    # Capability #2 (VM/AOIP lane) — clears leftover systemd "failed" state
    # only, never starts/stops/restarts the unit. Lower blast radius than
    # restart_unit (zero downtime risk) but kept at LOW rather than inventing
    # a class below it — still goes through the same tier_gate/HITL path.
    "systemd.reset_failed": LOW,
    # Capability #3 (VM/AOIP lane) — first auto-remediation for the
    # SYS_RESOURCE lane (journal disk pressure). Deletes disposable journal
    # log data via the official `journalctl --vacuum-size=`, never touches
    # app/process state — still LOW like the other two VM recovery
    # capabilities, still gated by the same tier_gate/HITL path.
    "systemd.journal_vacuum": LOW,
    "k8s_create_or_patch_configmap": LOW,   # cố định (bỏ phân loại động — QĐ #3)
    "sandbox_cleanup": LOW,                 # dọn sandbox, không đụng cluster
    "k8s_scale_resource": MEDIUM,
    "k8s_scale_deployment": MEDIUM,
    "k8s_patch_resource": MEDIUM,
    "k8s_patch_configmap": MEDIUM,
    # RBAC mutation luôn security-sensitive → HIGH (đồng nhất k8s_patch_rbac);
    # least-privilege apply vẫn thay đổi quyền executor SA → buộc HITL ở auto tier.
    "k8s_apply_rbac_least_privilege": HIGH,
    # arbitrary execution → fail-closed HIGH
    "execute_shell_command": HIGH,
    "execute_in_sandbox": HIGH,
    "gated_allowlisted_execute": HIGH,
    "kubectl_cluster": HIGH,
    # dangerous_tools (đã ở DANGEROUS_TOOLS) — liệt kê tường minh cho rõ
    "k8s_delete_pod": HIGH,
    "k8s_delete_deployment": HIGH,
    "k8s_delete_pvc": HIGH,
    "k8s_patch_rbac": HIGH,
    "k8s_patch_secret": HIGH,
    "k8s_mutate_taint": HIGH,
}

# Bảng tĩnh đầy đủ (readonly + mutate) — dùng cho Admin UI matrix + lookup nhanh.
STATIC_RISK_CLASS: Final[dict[str, str]] = {
    **{t: READONLY for t in _READONLY_TOOLS},
    **_STATIC_MUTATE,
}


def is_dangerous(tool_name: str) -> bool:
    return tool_name in DANGEROUS_TOOLS


def _clamp_dangerous(tool_name: str, risk_class: str) -> str:
    """Bất biến: dangerous_tools không bao giờ < HIGH (kể cả override)."""
    if tool_name in DANGEROUS_TOOLS and _ORDER.get(risk_class, 0) < _ORDER[HIGH]:
        return HIGH
    return risk_class


def risk_class_of(tool_name: str, *, override: str | None = None) -> str:
    """Risk-class hiệu lực. Fail-closed: tool lạ → HIGH.

    Thứ tự: override (clamp dangerous) → bảng tĩnh → HIGH. ``override`` là giá trị
    đã đọc từ DB→cache (None = không override).
    """
    if override is not None:
        if override not in VALID_RISK_CLASSES:
            return HIGH  # override hỏng → fail-closed
        return _clamp_dangerous(tool_name, override)
    static = STATIC_RISK_CLASS.get(tool_name)
    if static is not None:
        return static
    return HIGH  # fail-closed cho tool chưa phân loại


def rank(risk_class: str) -> int:
    """Thứ hạng (READONLY=0 … HIGH=3). Lạ → HIGH-rank."""
    return _ORDER.get(risk_class, _ORDER[HIGH])
