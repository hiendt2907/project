"""Collect temporal evidence from Prometheus and inject into evidence narrative."""

from __future__ import annotations

import logging
import re
from typing import Any

from prober.temporal_evidence import TemporalEvidenceBlock
from workers.handler_context import WorkerHandlerContext

logger = logging.getLogger(__name__)


def _alert_type_from_batch(batch: list[dict[str, Any]]) -> str:
    """Extract alert type/name to determine which metrics to fetch."""
    if not batch:
        return ""
    first = batch[0]
    alert_hint = str(first.get("alert_hint") or "").lower()
    alertname = str(first.get("alertname") or "").lower()
    canonical = str(first.get("canonical_query_snippet") or "")

    full_context = f"{alert_hint} {alertname} {canonical}"
    return full_context.lower()


def _generate_dynamic_promql_queries(
    alert_context: str,
    pod: str = "",
    namespace: str = "",
    deployment: str = "",
) -> dict[str, tuple[str, str, int]]:
    """
    Generate PromQL queries based on alert context.
    Returns: {metric_name: (promql_query, human_label, step_seconds), ...}

    Maps alert keywords to appropriate metrics:
    - "disk", "space" -> disk I/O, filesystem usage
    - "memory", "oom" -> memory usage, pressure
    - "cpu" -> CPU utilization
    - "5xx", "error", "latency" -> API/web metrics
    - "connection", "tcp" -> network metrics
    """
    queries = {}

    # Always include CPU for workload alerts (standard baseline)
    if pod and namespace:
        queries["pod_cpu_percent"] = (
            f'rate(container_cpu_usage_seconds_total{{pod="{pod}", namespace="{namespace}"}}[1m])*100',
            "Pod CPU Utilization (%)",
            60,
        )

    # Always include Memory for workload alerts
    if pod and namespace:
        queries["pod_memory_mb"] = (
            f'container_memory_usage_bytes{{pod="{pod}", namespace="{namespace}"}}/1024/1024',
            "Pod Memory Usage (MB)",
            60,
        )

    # Disk-related alerts
    if re.search(r"disk|storage|space|filesystem|io", alert_context):
        if pod and namespace:
            queries["pod_disk_io_read"] = (
                f'rate(container_fs_reads_bytes_total{{pod="{pod}", namespace="{namespace}"}}[1m])',
                "Disk Read Rate (bytes/sec)",
                30,
            )
            queries["pod_disk_io_write"] = (
                f'rate(container_fs_writes_bytes_total{{pod="{pod}", namespace="{namespace}"}}[1m])',
                "Disk Write Rate (bytes/sec)",
                30,
            )

    # Memory/OOM alerts (add memory pressure metrics)
    if re.search(r"memory|oom|oomkilled", alert_context):
        if pod and namespace:
            queries["pod_memory_rss"] = (
                f'container_memory_rss{{pod="{pod}", namespace="{namespace}"}}/1024/1024',
                "RSS Memory (MB)",
                60,
            )
        if namespace:
            queries["memory_pressure"] = (
                f'kube_pod_container_status_last_state_terminated_reason{{reason="OOMKilled", namespace="{namespace}"}}',
                "OOM Killed Pods",
                300,
            )

    # API/5xx error alerts
    if re.search(r"5xx|error|http.*error|api.*error", alert_context):
        if deployment and namespace:
            queries["error_rate"] = (
                f'rate(http_requests_total{{status=~"5..", deployment="{deployment}", namespace="{namespace}"}}[1m])',
                "HTTP 5xx Error Rate",
                30,
            )
            queries["request_latency_p99"] = (
                f'histogram_quantile(0.99, rate(http_request_duration_seconds_bucket{{deployment="{deployment}", namespace="{namespace}"}}[1m]))',
                "HTTP Latency P99 (sec)",
                30,
            )

    # Network/connection alerts
    if re.search(r"connection|network|tcp|timeout", alert_context):
        if pod and namespace:
            queries["tcp_connections"] = (
                f'increase(tcp_connections_total{{pod="{pod}", namespace="{namespace}"}}[1m])',
                "TCP Connections",
                60,
            )

    # Service-level SLI fallback — for SIEM alerts where pod is empty but deployment+namespace known
    if deployment and namespace and not pod:
        queries["svc_error_rate"] = (
            f'sum(rate(http_requests_total{{job="{deployment}", namespace="{namespace}", status=~"5.."}}[5m]))'
            f' / sum(rate(http_requests_total{{job="{deployment}", namespace="{namespace}"}}[5m]))',
            "Service Error Rate (5xx ratio)",
            60,
        )
        queries["svc_latency_p99"] = (
            f'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{{job="{deployment}", namespace="{namespace}"}}[5m])) by (le))',
            "Service Latency P99 (sec)",
            60,
        )

    # Always-on cluster throughput baseline (excludes infra namespaces)
    queries["cluster_throughput_rps"] = (
        'sum(rate(http_requests_total{namespace!~"kube-.*|monitor|kube-system"}[5m]))',
        "Cluster HTTP Throughput (req/sec)",
        60,
    )

    # Replica/scaling alerts
    if re.search(r"replica|scaling|pending|unschedulable", alert_context) and deployment and namespace:
        queries["deployment_replicas"] = (
            f'kube_deployment_status_replicas_available{{deployment="{deployment}", namespace="{namespace}"}}',
            "Available Replicas",
            300,
        )
        queries["deployment_replicas_desired"] = (
            f'kube_deployment_spec_replicas{{deployment="{deployment}", namespace="{namespace}"}}',
            "Desired Replicas",
            300,
        )

    return queries





async def fetch_temporal_evidence_for_batch(
    ctx: WorkerHandlerContext,
    batch: list[dict[str, Any]],
    trace: str,
) -> str:
    """
    Fetch 1-hour historical metrics from Prometheus for batch evidence.
    Returns a [TEMPORAL_EVIDENCE ...] block to inject into the evidence narrative.

    Dynamically selects metrics based on alert context (disk, CPU, memory, 5xx, etc.)
    instead of always fetching only CPU/memory/replicas.
    """
    if not batch:
        return ""

    # Extract workload info from first batch entry
    first = batch[0] if batch else {}
    namespace = str(first.get("namespace") or "unknown").strip()
    pod = str(first.get("pod_name") or "").strip()
    deployment = str(first.get("deployment") or "").strip()

    prometheus_url = getattr(ctx.settings, "prometheus_url", "http://prometheus:9090")
    if not prometheus_url:
        logger.debug("event=temporal_evidence_prometheus_disabled")
        return ""

    # Generate context-aware PromQL queries based on alert type
    alert_context = _alert_type_from_batch(batch)
    dynamic_queries = _generate_dynamic_promql_queries(
        alert_context,
        pod=pod,
        namespace=namespace,
        deployment=deployment,
    )

    if not dynamic_queries:
        logger.debug("event=temporal_evidence_no_queries_for_alert alert_context=%s", alert_context[:100])
        return ""

    blocks = []
    for metric_name, (promql_query, human_label, step_sec) in dynamic_queries.items():
        try:
            block = await TemporalEvidenceBlock.fetch_from_prometheus(
                prometheus_url,
                promql_query,
                metric_name,
                hours_back=1,
                step=f"{step_sec}s",
                timeout=5.0,
            )
            if block:
                blocks.append(block.to_prompt_block())
                logger.debug(
                    "event=temporal_evidence_fetched metric=%s label=%s samples=%s trace=%s",
                    metric_name,
                    human_label,
                    block.sample_points,
                    trace,
                )
        except Exception as e:
            logger.warning(
                "event=temporal_evidence_fetch_error metric=%s alert_context=%s err=%s",
                metric_name,
                alert_context[:80],
                str(e)[:100],
            )

    if not blocks:
        logger.info(
            "event=temporal_evidence_no_data_collected alert_context=%s trace=%s",
            alert_context[:100],
            trace,
        )
        return ""

    result = "\n".join(blocks)
    logger.info(
        "event=temporal_evidence_collected trace=%s alert_context=%s blocks=%s",
        trace,
        alert_context[:100],
        len(blocks),
    )
    return result
