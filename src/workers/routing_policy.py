"""Tool được phép auto-execute trên fast-path (SOP + routing experience học từ slow-path)."""

from __future__ import annotations

from workers.settings import WorkerSettings

# Đồng bộ với seed SOP — chỉ read-only / an toàn; không rollout/sandbox shell.
READ_ONLY_FAST_PATH_TOOLS: frozenset[str] = frozenset(
    {
        "pgvector_health",
        "pgvector_status",
        "pgvector_health_audit",
        "redis_health",
        "redis_info",
        "redis_expert_check",
        "system_psutil",
        "system_psutil_diskio",
        "k8s_list_pods",
        "list_namespace_pods",
        "list_all_pods_sdk",
        "resolve_pod_identity",
        "resolve_deployment_identity",
        "namespace_pods_top",
        "promql_instant",
        "promql_range",
        "vm_promql_instant",
        "vm_promql_range",
        "metrics_promql_hints",
        "query_prometheus_metrics",
        "query_victoria_metrics",
        "query_vm_timeseries",
        "query_historical_metrics",
        "get_historical_series_dataframe",
        "forecast_metric_prophet",
        "audit_observability_stack",
        "net_scapy_interfaces",
        "postgres_ping",
        "forecast_memory_risk_vm",
        "predict_resource_exhaustion",
        "timeseries_analyze",
        "echo",
        "reply",
    }
)

# God / lab_unchained — thêm shell & sandbox có trong TOOL_REGISTRY (không mở rollout).
GOD_MODE_FAST_PATH_EXTRA_TOOLS: frozenset[str] = frozenset(
    {
        "execute_shell_command",
        "execute_in_sandbox",
        "gated_allowlisted_execute",
    }
)

ROUTING_SOURCE_SLOW_PATH = "slow_path_success"
# Agentic ReAct session chốt bằng omni_mark_resolved — học deferred (không ghi giữa vòng).
ROUTING_SOURCE_AGENT_SESSION = "agent_session_resolved"
# Ghi khi hết vòng slow-path — không đưa vào context retrieval (tránh nhiễu).
ROUTING_SOURCE_SLOW_PATH_EXHAUSTED = "slow_path_exhausted"

# Fast-path auto_execute: chỉ nguồn học đã xác minh (legacy slow-path + agentic playbook).
ROUTING_SOURCES_FAST_PATH_EXECUTE: frozenset[str] = frozenset(
    {ROUTING_SOURCE_SLOW_PATH, ROUTING_SOURCE_AGENT_SESSION}
)


def shell_fast_path_enabled(ws: WorkerSettings | None) -> bool:
    if ws is None:
        return False
    return bool(ws.god_mode or ws.lab_unchained)


def fast_path_auto_execute_allowlist(ws: WorkerSettings | None) -> frozenset[str]:
    if shell_fast_path_enabled(ws):
        return READ_ONLY_FAST_PATH_TOOLS | GOD_MODE_FAST_PATH_EXTRA_TOOLS
    return READ_ONLY_FAST_PATH_TOOLS


def is_fast_path_auto_allowed(tool: str, ws: WorkerSettings | None = None) -> bool:
    return (tool or "").strip() in fast_path_auto_execute_allowlist(ws)
