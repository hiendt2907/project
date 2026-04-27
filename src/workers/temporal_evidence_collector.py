"""Collect temporal evidence from Prometheus and inject into evidence narrative."""

from __future__ import annotations

import logging
from typing import Any

from prober.temporal_evidence import TemporalEvidenceBlock
from workers.handler_context import WorkerHandlerContext

logger = logging.getLogger(__name__)


async def fetch_temporal_evidence_for_batch(
    ctx: WorkerHandlerContext,
    batch: list[dict[str, Any]],
    trace: str,
) -> str:
    """
    Fetch 1-hour historical metrics from Prometheus for batch evidence.
    Returns a [TEMPORAL_EVIDENCE ...] block to inject into the evidence narrative.

    Extracts relevant metrics from batch labels (pod, deployment, namespace)
    and fetches historical data via Prometheus query_range.
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

    blocks = []

    # Fetch CPU utilization for pod (if pod name available)
    if pod and namespace:
        cpu_query = f'rate(container_cpu_usage_seconds_total{{pod="{pod}", namespace="{namespace}"}}[1m])*100'
        try:
            block = await TemporalEvidenceBlock.fetch_from_prometheus(
                prometheus_url,
                cpu_query,
                "pod_cpu_percent",
                hours_back=1,
                step="60s",
                timeout=5.0,
            )
            if block:
                blocks.append(block.to_prompt_block())
                logger.debug(
                    "event=temporal_evidence_cpu_fetched pod=%s namespace=%s samples=%s",
                    pod,
                    namespace,
                    block.sample_points,
                )
        except Exception as e:
            logger.warning(
                "event=temporal_evidence_cpu_fetch_error pod=%s err=%s",
                pod,
                str(e)[:100],
            )

    # Fetch memory for pod
    if pod and namespace:
        mem_query = f'container_memory_usage_bytes{{pod="{pod}", namespace="{namespace}"}}/1024/1024'
        try:
            block = await TemporalEvidenceBlock.fetch_from_prometheus(
                prometheus_url,
                mem_query,
                "pod_memory_mb",
                hours_back=1,
                step="60s",
                timeout=5.0,
            )
            if block:
                blocks.append(block.to_prompt_block())
                logger.debug(
                    "event=temporal_evidence_memory_fetched pod=%s namespace=%s",
                    pod,
                    namespace,
                )
        except Exception as e:
            logger.warning(
                "event=temporal_evidence_memory_fetch_error pod=%s err=%s",
                pod,
                str(e)[:100],
            )

    # Fetch deployment replica count (if deployment available)
    if deployment and namespace:
        replica_query = f'kube_deployment_status_replicas_available{{deployment="{deployment}", namespace="{namespace}"}}'
        try:
            block = await TemporalEvidenceBlock.fetch_from_prometheus(
                prometheus_url,
                replica_query,
                "deployment_replicas_available",
                hours_back=1,
                step="300s",  # Lower frequency for replica counts
                timeout=5.0,
            )
            if block:
                blocks.append(block.to_prompt_block())
                logger.debug(
                    "event=temporal_evidence_replicas_fetched deployment=%s namespace=%s",
                    deployment,
                    namespace,
                )
        except Exception as e:
            logger.warning(
                "event=temporal_evidence_replicas_fetch_error deployment=%s err=%s",
                deployment,
                str(e)[:100],
            )

    if not blocks:
        return ""

    result = "\n".join(blocks)
    logger.info(
        "event=temporal_evidence_collected trace=%s blocks=%s",
        trace,
        len(blocks),
    )
    return result
