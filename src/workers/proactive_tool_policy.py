"""Canonical proactive ReAct tool sets by phase (intersect with allowed_tools in observer)."""

from __future__ import annotations

# Must stay in sync with TOOL_REGISTRY registration names.
PROACTIVE_DIAGNOSE_TOOLS: frozenset[str] = frozenset(
    {
        "inspect_pod_details",
        "inspect_pod_deep",
        "k8s_list_pods",
        "list_namespace_pods",
        "list_all_pods_sdk",
        "promql_instant",
        "query_prometheus_metrics",
        "redis_health",
        "redis_info",
        "resolve_pod_identity",
        "resolve_deployment_identity",
    }
)

PROACTIVE_RECHECK_TOOLS: frozenset[str] = frozenset(
    {
        "promql_instant",
        "query_prometheus_metrics",
        "k8s_list_pods",
        "redis_health",
    }
)
