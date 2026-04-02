"""Registry tool — Service Provider: SDK-only (psutil, kubernetes_asyncio, httpx, matplotlib, scapy, asyncpg)."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from workers import k8s_cluster_tools as _k8s_cluster_tools  # noqa: F401 — @register_tool side effects
from workers import kubectl_cluster as _kubectl_cluster  # noqa: F401
from workers import k8s_readonly_tools as _k8s_readonly_tools  # noqa: F401 — register_tool side effects
from workers.tool_registry import get_tool_registry

from workers.k8s_tools import (
    tool_inspect_pod_deep,
    tool_inspect_pod_details,
    tool_k8s_list_pods,
    tool_k8s_rollout_restart,
    tool_list_all_pods_sdk,
    tool_list_namespace_pods,
    tool_namespace_pods_top,
    tool_resolve_deployment_identity,
    tool_resolve_pod_identity,
)
from workers.observability_audit import tool_audit_observability_stack
from workers.gated_execute import tool_gated_allowlisted_execute
from workers.lab_shell import tool_execute_shell_command
from workers.sandbox_tools import tool_execute_in_sandbox, tool_sandbox_cleanup
from workers.sdk_service_tools import (
    tool_forecast_memory_risk_vm,
    tool_forecast_metric_prophet,
    tool_get_historical_series_dataframe,
    tool_metrics_promql_hints,
    tool_net_scapy_interfaces,
    tool_postgres_ping,
    tool_vendor_knowledge_search,
    tool_predict_resource_exhaustion,
    tool_pgvector_health,
    tool_pgvector_health_audit,
    tool_pgvector_status,
    tool_query_historical_metrics,
    tool_query_prometheus_metrics,
    tool_query_vm_timeseries,
    tool_query_victoria_metrics,
    tool_redis_expert_check,
    tool_redis_health,
    tool_redis_info,
    tool_system_psutil,
    tool_system_psutil_diskio,
    tool_timeseries_analyze,
    tool_viz_line_chart,
    tool_viz_vm_range_chart,
    tool_promql_instant,
    tool_promql_range,
    tool_vm_promql_instant,
    tool_vm_promql_range,
)

ToolFn = Callable[[Any, dict[str, Any]], Awaitable[str]]

TOOL_REGISTRY: dict[str, ToolFn] = {}


class ToolCallPayload(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


def register_tool(name: str, fn: ToolFn) -> None:
    TOOL_REGISTRY[name] = fn


def format_unknown_tool_feedback_en(bad_tool: str, *, unattended: bool) -> str:
    """English-only correction for LLM hallucinated tool names; lists registered names so the ReAct loop can continue."""
    names = sorted(TOOL_REGISTRY.keys())
    if unattended:
        names = [n for n in names if n != "reply"]
    catalog = ", ".join(f"`{n}`" for n in names)
    max_chars = 12_000
    if len(catalog) > max_chars:
        catalog = catalog[: max_chars - 24] + "... [truncated]"
    return (
        f"[SYSTEM] Tool `{bad_tool}` is not registered (not in TOOL_REGISTRY). "
        f"Cause: typo or hallucinated tool name. "
        f"Respond with exactly one JSON object; use only **registered** tool names: {catalog}. "
        f"Do not invent new tools. If you cannot proceed, use `escalate_to_human`. "
        f"`omni_mark_resolved.args.summary` must be ≤ 25 words, English only."
    )


async def tool_echo(ctx: Any, args: dict[str, Any]) -> str:
    """Lặp lại đúng chuỗi (ops/debug)."""
    return str(args.get("msg", ""))


async def tool_reply(ctx: Any, args: dict[str, Any]) -> str:
    """Trả lời text (hội thoại)."""
    from workers.ollama_prompts_en import truncate_plain_text_to_max_words

    t = truncate_plain_text_to_max_words(str(args.get("text") or args.get("message") or ""))
    return t or "(empty)"


async def tool_escalate_to_human(ctx: Any, args: dict[str, Any]) -> str:
    """Đánh dấu leo thang; audit/Telegram/thoát ReAct do agentic_slow_path xử lý."""
    r = str(args.get("reason") or "").strip()
    return f"[ESCALATED] {r}" if r else "[ESCALATED]"


register_tool("echo", tool_echo)
register_tool("reply", tool_reply)
register_tool("escalate_to_human", tool_escalate_to_human)


async def tool_omni_mark_resolved(ctx: Any, args: dict[str, Any]) -> str:
    """Đóng phiên agentic — playbook học từ trajectory (không auto trên fast-path)."""
    from workers.ollama_prompts_en import truncate_plain_text_to_max_words

    summary = truncate_plain_text_to_max_words(str(args.get("summary") or args.get("text") or ""))
    setattr(ctx, "_agentic_session_resolved", True)
    setattr(ctx, "_agentic_resolve_summary", summary)
    return f"[RESOLVED] {summary}" if summary else "[RESOLVED]"


register_tool("omni_mark_resolved", tool_omni_mark_resolved)
register_tool("list_namespace_pods", tool_list_namespace_pods)
register_tool("namespace_pods_top", tool_namespace_pods_top)
register_tool("list_all_pods_sdk", tool_list_all_pods_sdk)
register_tool("resolve_pod_identity", tool_resolve_pod_identity)
register_tool("resolve_deployment_identity", tool_resolve_deployment_identity)
register_tool("inspect_pod_deep", tool_inspect_pod_deep)
register_tool("inspect_pod_details", tool_inspect_pod_details)
register_tool("k8s_list_pods", tool_k8s_list_pods)
register_tool("k8s_rollout_restart", tool_k8s_rollout_restart)
register_tool("system_psutil", tool_system_psutil)
register_tool("system_psutil_diskio", tool_system_psutil_diskio)
register_tool("promql_instant", tool_promql_instant)
register_tool("promql_range", tool_promql_range)
register_tool("vm_promql_instant", tool_vm_promql_instant)
register_tool("vm_promql_range", tool_vm_promql_range)
register_tool("metrics_promql_hints", tool_metrics_promql_hints)
register_tool("timeseries_analyze", tool_timeseries_analyze)
register_tool("viz_line_chart", tool_viz_line_chart)
register_tool("viz_vm_range_chart", tool_viz_vm_range_chart)
register_tool("query_historical_metrics", tool_query_historical_metrics)
register_tool("get_historical_series_dataframe", tool_get_historical_series_dataframe)
register_tool("forecast_metric_prophet", tool_forecast_metric_prophet)
register_tool("query_prometheus_metrics", tool_query_prometheus_metrics)
register_tool("query_victoria_metrics", tool_query_victoria_metrics)
register_tool("query_vm_timeseries", tool_query_vm_timeseries)
register_tool("forecast_memory_risk_vm", tool_forecast_memory_risk_vm)
register_tool("predict_resource_exhaustion", tool_predict_resource_exhaustion)
register_tool("redis_expert_check", tool_redis_expert_check)
register_tool("pgvector_health_audit", tool_pgvector_health_audit)
register_tool("redis_health", tool_redis_health)
register_tool("redis_info", tool_redis_info)
register_tool("pgvector_health", tool_pgvector_health)
register_tool("pgvector_status", tool_pgvector_status)
register_tool("net_scapy_interfaces", tool_net_scapy_interfaces)
register_tool("postgres_ping", tool_postgres_ping)
register_tool("vendor_knowledge_search", tool_vendor_knowledge_search)
register_tool("audit_observability_stack", tool_audit_observability_stack)
register_tool("execute_in_sandbox", tool_execute_in_sandbox)
register_tool("gated_allowlisted_execute", tool_gated_allowlisted_execute)
register_tool("sandbox_cleanup", tool_sandbox_cleanup)
register_tool("execute_shell_command", tool_execute_shell_command)


def _bind_registry_tools(names: tuple[str, ...]) -> None:
    for nm in names:

        def _make(n: str):
            async def _fn(ctx: Any, args: dict[str, Any]) -> str:
                return await get_tool_registry().invoke(ctx, n, args)

            return _fn

        TOOL_REGISTRY[nm] = _make(nm)


_bind_registry_tools(
    (
        "k8s_scale_deployment",
        "k8s_describe_resource",
        "k8s_tail_logs",
        "k8s_check_endpoints",
        "k8s_patch_resource",
        "k8s_list_nodes",
        "k8s_node_conditions",
        "k8s_list_services",
        "k8s_list_ingress",
        "kubectl_cluster",
    )
)
