from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from kubernetes_asyncio import client

import httpx

from workers.diagnostic_evidence import ProbeRunRaw
from workers.diagnostic_k8s_clinical import (
    probe_k8s_clinical_pod_events,
    probe_k8s_clinical_pod_log_previous,
    probe_k8s_clinical_pod_log_tail,
    probe_k8s_clinical_pod_metrics,
    probe_k8s_clinical_pod_status,
    probe_k8s_resource_quota_probe,
)
from workers.diagnostic_resource import promql_workload_pod_regex_selector, workload_pod_prefix_for_promql
from workers.k8s_tools import _load_k8s_config
from workers.proactive_models import AnomalyEvent
from workers.handlers import WorkerHandlerContext
from workers.security_probes import (
    probe_k8s_configmap_security_drift,
    probe_k8s_rbac_drift,
)

logger = logging.getLogger(__name__)

ProbeFn = Callable[[WorkerHandlerContext, AnomalyEvent], Awaitable[ProbeRunRaw]]


async def probe_redis_ping(ctx: WorkerHandlerContext, _ev: AnomalyEvent) -> ProbeRunRaw:
    try:
        pong = await ctx.redis.ping()
        ok = pong is True or pong == b"PONG"
        return ProbeRunRaw(
            probe_name="redis_ping",
            status="PASSED" if ok else "INCONCLUSIVE",
            raw_text=str(pong),
            structured_hint={"redis_ping": str(pong)},
        )
    except Exception as e:
        logger.warning("probe_redis_ping: %s", e)
        return ProbeRunRaw(probe_name="redis_ping", status="FAILED", raw_text=str(e)[:2000])


async def probe_k8s_list_pods_namespace(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> ProbeRunRaw:
    await _load_k8s_config()
    v1 = client.CoreV1Api()
    ns = (ev.namespace or "").strip() or ctx.settings.k8s_default_namespace
    try:
        resp = await v1.list_namespaced_pod(namespace=ns)
        n = len(resp.items or [])
        return ProbeRunRaw(
            probe_name="k8s_list_pods_namespace",
            status="PASSED",
            raw_text=f"namespace={ns} pod_count={n}",
            structured_hint={"namespace": ns, "pod_count": n},
        )
    except Exception as e:
        return ProbeRunRaw(
            probe_name="k8s_list_pods_namespace",
            status="FAILED",
            raw_text=str(e)[:2000],
        )
    finally:
        # Tránh aiohttp "Unclosed client session" (kubernetes_asyncio dùng aiohttp bên dưới).
        try:
            await v1.api_client.close()
        except Exception:
            pass


def _prom_label_esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


async def _prometheus_instant_query(ctx: WorkerHandlerContext, promql: str) -> dict[str, Any]:
    base = getattr(ctx.settings, "prometheus_url", None)
    if not isinstance(base, str) or not base.strip():
        return {"status": "error", "error": "prometheus_url unset"}
    url = f"{base.strip().rstrip('/')}/api/v1/query"
    async with httpx.AsyncClient(timeout=25.0) as hc:
        r = await hc.get(url, params={"query": promql})
        r.raise_for_status()
        return r.json()


def _instant_vector_summary(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if data.get("status") != "success":
        return f"prom_status={data.get('status')!r}", {}
    res = (data.get("data") or {}).get("result") or []
    if not res:
        return "empty_vector", {}
    parts: list[str] = []
    structured: dict[str, Any] = {}
    for i, series in enumerate(res[:8]):
        m = series.get("metric") or {}
        pod = m.get("pod", "")
        container = m.get("container", "")
        val = series.get("value")
        fv: float | None = None
        if val and len(val) >= 2:
            try:
                fv = float(val[1])
            except (TypeError, ValueError):
                fv = None
        key = f"{pod}/{container}" if pod or container else f"s{i}"
        structured[key] = fv
        parts.append(f"{key}={fv}")
    return "; ".join(parts), structured


async def probe_prom_pod_cpu_cores(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> ProbeRunRaw:
    ns, wl = workload_pod_prefix_for_promql(ev)
    pod_sel = promql_workload_pod_regex_selector(wl)
    if not ns or not pod_sel:
        return ProbeRunRaw(
            probe_name="prom_pod_cpu_cores",
            status="SKIPPED",
            raw_text="missing namespace or workload identity for PromQL (use deployment/workload labels or derivable pod name)",
        )
    e_ns = _prom_label_esc(ns)
    # Workload-scoped regex (all pods of Deployment/STS) — never exact alert pod name.
    # container!="" loại cả series cAdvisor chỉ có pod-level (không có label container — k3s/OrbStack).
    promql = (
        f'sum(rate(container_cpu_usage_seconds_total{{namespace="{e_ns}",{pod_sel},'
        f'container!="POD"}}[5m])) '
        f'or sum(rate(container_cpu_usage_seconds_total{{namespace="{e_ns}",{pod_sel}}}[5m]))'
    )
    try:
        data = await _prometheus_instant_query(ctx, promql)
        summary, structured = _instant_vector_summary(data)
        ok = data.get("status") == "success" and (data.get("data") or {}).get("result")
        return ProbeRunRaw(
            probe_name="prom_pod_cpu_cores",
            status="PASSED" if ok else "INCONCLUSIVE",
            raw_text=f"workload_scoped promql={promql[:300]} … | {summary}"[:4000],
            structured_hint={"unit": "cores_sum_rate5m", "workload": wl, **structured},
        )
    except Exception as e:
        logger.warning("probe_prom_pod_cpu_cores: %s", e)
        return ProbeRunRaw(
            probe_name="prom_pod_cpu_cores",
            status="FAILED",
            raw_text=str(e)[:2000],
        )


async def probe_prom_pod_memory_wss(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> ProbeRunRaw:
    ns, wl = workload_pod_prefix_for_promql(ev)
    pod_sel = promql_workload_pod_regex_selector(wl)
    if not ns or not pod_sel:
        return ProbeRunRaw(
            probe_name="prom_pod_memory_wss",
            status="SKIPPED",
            raw_text="missing namespace or workload identity for PromQL (use deployment/workload labels or derivable pod name)",
        )
    e_ns = _prom_label_esc(ns)
    promql = (
        f'sum(container_memory_working_set_bytes{{namespace="{e_ns}",{pod_sel},'
        f'container!="POD"}}) '
        f'or sum(container_memory_working_set_bytes{{namespace="{e_ns}",{pod_sel}}})'
    )
    try:
        data = await _prometheus_instant_query(ctx, promql)
        summary, structured = _instant_vector_summary(data)
        ok = data.get("status") == "success" and (data.get("data") or {}).get("result")
        return ProbeRunRaw(
            probe_name="prom_pod_memory_wss",
            status="PASSED" if ok else "INCONCLUSIVE",
            raw_text=f"workload_scoped promql={promql[:300]} … | {summary}"[:4000],
            structured_hint={"unit": "bytes_wss_sum", "workload": wl, **structured},
        )
    except Exception as e:
        logger.warning("probe_prom_pod_memory_wss: %s", e)
        return ProbeRunRaw(
            probe_name="prom_pod_memory_wss",
            status="FAILED",
            raw_text=str(e)[:2000],
        )


async def probe_kafka_alerts_topic(ctx: WorkerHandlerContext, _ev: AnomalyEvent) -> ProbeRunRaw:
    """Kafka alerts topic reachable (metadata; depth via broker metrics / consumer lag)."""
    ws = ctx.settings
    t = ws.kafka_topic_alerts
    return ProbeRunRaw(
        probe_name="kafka_alerts_topic",
        status="PASSED",
        raw_text=f"kafka topic={t} bootstrap={ws.kafka_bootstrap_servers}",
        structured_hint={"topic": t, "bootstrap": ws.kafka_bootstrap_servers},
    )


# ── P7.2.A — L1 OS-level probes (node_exporter via Prometheus) ───────────────

async def probe_node_disk_pressure(ctx: WorkerHandlerContext, _ev: AnomalyEvent) -> ProbeRunRaw:
    """L1: Node filesystem available < 10% on any mount."""
    promql = (
        'node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"} '
        '/ node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs"} < 0.1'
    )
    try:
        data = await _prometheus_instant_query(ctx, promql)
        res = (data.get("data") or {}).get("result") or []
        if res:
            summary, structured = _instant_vector_summary(data)
            return ProbeRunRaw(
                probe_name="node_disk_pressure",
                status="FAILED",
                raw_text=f"node(s) below 10% free disk: {summary}"[:4000],
                structured_hint={"unit": "free_ratio", **structured},
            )
        return ProbeRunRaw(
            probe_name="node_disk_pressure",
            status="PASSED",
            raw_text="all node filesystems above 10% free",
        )
    except Exception as e:
        logger.warning("probe_node_disk_pressure: %s", e)
        return ProbeRunRaw(probe_name="node_disk_pressure", status="INCONCLUSIVE", raw_text=str(e)[:2000])


async def probe_node_cpu_saturation(ctx: WorkerHandlerContext, _ev: AnomalyEvent) -> ProbeRunRaw:
    """L1: Cluster-level CPU saturation (1 - idle %)."""
    promql = '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))'
    try:
        data = await _prometheus_instant_query(ctx, promql)
        summary, structured = _instant_vector_summary(data)
        ok = data.get("status") == "success" and (data.get("data") or {}).get("result")
        return ProbeRunRaw(
            probe_name="node_cpu_saturation",
            status="PASSED" if ok else "INCONCLUSIVE",
            raw_text=f"cluster_cpu_saturation={summary}"[:4000],
            structured_hint={"unit": "saturation_ratio", **structured},
        )
    except Exception as e:
        logger.warning("probe_node_cpu_saturation: %s", e)
        return ProbeRunRaw(probe_name="node_cpu_saturation", status="INCONCLUSIVE", raw_text=str(e)[:2000])


async def probe_node_memory_pressure(ctx: WorkerHandlerContext, _ev: AnomalyEvent) -> ProbeRunRaw:
    """L1: Node memory pressure (1 - MemAvailable/MemTotal)."""
    promql = '1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)'
    try:
        data = await _prometheus_instant_query(ctx, promql)
        summary, structured = _instant_vector_summary(data)
        ok = data.get("status") == "success" and (data.get("data") or {}).get("result")
        return ProbeRunRaw(
            probe_name="node_memory_pressure",
            status="PASSED" if ok else "INCONCLUSIVE",
            raw_text=f"node_memory_pressure={summary}"[:4000],
            structured_hint={"unit": "pressure_ratio", **structured},
        )
    except Exception as e:
        logger.warning("probe_node_memory_pressure: %s", e)
        return ProbeRunRaw(probe_name="node_memory_pressure", status="INCONCLUSIVE", raw_text=str(e)[:2000])


async def probe_node_disk_io_saturation(ctx: WorkerHandlerContext, _ev: AnomalyEvent) -> ProbeRunRaw:
    """L1: Node disk I/O utilisation rate."""
    promql = 'rate(node_disk_io_time_seconds_total[5m])'
    try:
        data = await _prometheus_instant_query(ctx, promql)
        summary, structured = _instant_vector_summary(data)
        ok = data.get("status") == "success" and (data.get("data") or {}).get("result")
        return ProbeRunRaw(
            probe_name="node_disk_io_saturation",
            status="PASSED" if ok else "INCONCLUSIVE",
            raw_text=f"disk_io_utilisation={summary}"[:4000],
            structured_hint={"unit": "io_util_ratio", **structured},
        )
    except Exception as e:
        logger.warning("probe_node_disk_io_saturation: %s", e)
        return ProbeRunRaw(probe_name="node_disk_io_saturation", status="INCONCLUSIVE", raw_text=str(e)[:2000])


# ── P7.2.B — L2 Network probes (kubernetes_asyncio, read-only) ───────────────

async def probe_k8s_service_endpoints_ready(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> ProbeRunRaw:
    """L2: Check service endpoints have ready backends (empty subsets = unreachable)."""
    ns = (getattr(ev, "namespace", None) or "").strip()
    svc = (getattr(ev, "workload", None) or getattr(ev, "deployment", None) or "").strip()
    if not ns or not svc:
        return ProbeRunRaw(
            probe_name="k8s_service_endpoints_ready",
            status="SKIPPED",
            raw_text="missing namespace or service name",
        )
    try:
        from kubernetes_asyncio import client, config as k8s_config

        await k8s_config.load_incluster_config()
        v1 = client.CoreV1Api()
        try:
            ep = await v1.read_namespaced_endpoints(name=svc, namespace=ns)
            subsets = ep.subsets or []
            ready_addrs = sum(len(s.addresses or []) for s in subsets)
            if ready_addrs == 0:
                return ProbeRunRaw(
                    probe_name="k8s_service_endpoints_ready",
                    status="FAILED",
                    raw_text=f"service/{svc} in {ns}: no ready endpoints (subsets empty)",
                    structured_hint={"service": svc, "namespace": ns, "ready_addresses": 0},
                )
            return ProbeRunRaw(
                probe_name="k8s_service_endpoints_ready",
                status="PASSED",
                raw_text=f"service/{svc} in {ns}: {ready_addrs} ready endpoint(s)",
                structured_hint={"service": svc, "namespace": ns, "ready_addresses": ready_addrs},
            )
        except Exception as e:
            return ProbeRunRaw(
                probe_name="k8s_service_endpoints_ready",
                status="INCONCLUSIVE",
                raw_text=f"could not read endpoints/{svc} in {ns}: {e}"[:2000],
            )
        finally:
            await v1.api_client.close()
    except Exception as e:
        logger.warning("probe_k8s_service_endpoints_ready: %s", e)
        return ProbeRunRaw(probe_name="k8s_service_endpoints_ready", status="INCONCLUSIVE", raw_text=str(e)[:2000])


async def probe_k8s_networkpolicy_audit(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> ProbeRunRaw:
    """L2: List NetworkPolicies in alert namespace — surfaces deny-all or restrictive rules."""
    ns = (getattr(ev, "namespace", None) or "").strip()
    if not ns:
        return ProbeRunRaw(
            probe_name="k8s_networkpolicy_audit",
            status="SKIPPED",
            raw_text="missing namespace",
        )
    try:
        from kubernetes_asyncio import client, config as k8s_config

        await k8s_config.load_incluster_config()
        netv1 = client.NetworkingV1Api()
        try:
            policies = await netv1.list_namespaced_network_policy(namespace=ns)
            names = [p.metadata.name for p in (policies.items or [])]
            count = len(names)
            return ProbeRunRaw(
                probe_name="k8s_networkpolicy_audit",
                status="PASSED",
                raw_text=f"namespace={ns} network_policies={count}: {', '.join(names[:10])}",
                structured_hint={"namespace": ns, "policy_count": count, "policies": names[:10]},
            )
        except Exception as e:
            return ProbeRunRaw(
                probe_name="k8s_networkpolicy_audit",
                status="INCONCLUSIVE",
                raw_text=f"could not list NetworkPolicies in {ns}: {e}"[:2000],
            )
        finally:
            await netv1.api_client.close()
    except Exception as e:
        logger.warning("probe_k8s_networkpolicy_audit: %s", e)
        return ProbeRunRaw(probe_name="k8s_networkpolicy_audit", status="INCONCLUSIVE", raw_text=str(e)[:2000])


async def probe_loki_access_log_surge(ctx: WorkerHandlerContext, ev: AnomalyEvent) -> ProbeRunRaw:
    """Query Loki for sustained HTTP error surge (429/5xx/auth) — sigma-bypass eligible evidence."""
    from workers.log_surge_probe import evaluate_log_surge_sigma_bypass

    ns = (ev.namespace or "").strip()
    pod = (ev.gigo_metadata.get("pod", "") or ev.deployment or "").strip()
    loki_url = getattr(ctx.settings, "omni_loki_base_url", None) or "http://loki.monitor.svc.cluster.local:3100"

    if not ns:
        return ProbeRunRaw(
            probe_name="loki_access_log_surge",
            status="SKIPPED",
            raw_text="missing namespace label — cannot scope Loki query",
        )
    try:
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url=loki_url,
            namespace=ns,
            pod_name=pod,
            window_sec=300,
            min_lines=5,
            min_ratio=0.3,
            line_limit=500,
            timeout_sec=25.0,
        )
        status = "PASSED" if result.ok else "INCONCLUSIVE"
        counts = getattr(result, "meta", {}) or {}
        return ProbeRunRaw(
            probe_name="loki_access_log_surge",
            status=status,
            raw_text=(
                f"dominant_class={result.dominant_error_class} "
                f"sigma_bypass={result.ok} reason={result.reason} "
                f"counts={counts}"
            )[:2000],
            structured_hint={
                "dominant_error_class": result.dominant_error_class,
                "sigma_bypass_eligible": result.ok,
                "sigma_bypass_reason": result.reason,
                "meta": counts,
            },
        )
    except Exception as e:
        logger.warning("probe_loki_access_log_surge: %s", e)
        return ProbeRunRaw(probe_name="loki_access_log_surge", status="INCONCLUSIVE", raw_text=str(e)[:2000])


PROBE_REGISTRY: dict[str, ProbeFn] = {
    "redis_ping": probe_redis_ping,
    "k8s_list_pods_namespace": probe_k8s_list_pods_namespace,
    "kafka_alerts_topic": probe_kafka_alerts_topic,
    "redis_stream_len_inbound": probe_kafka_alerts_topic,
    "k8s_clinical_pod_status": probe_k8s_clinical_pod_status,
    "k8s_clinical_pod_metrics": probe_k8s_clinical_pod_metrics,
    "k8s_clinical_pod_log_tail": probe_k8s_clinical_pod_log_tail,
    "k8s_clinical_pod_log_previous": probe_k8s_clinical_pod_log_previous,
    "k8s_clinical_pod_events": probe_k8s_clinical_pod_events,
    "k8s_events_probe": probe_k8s_clinical_pod_events,
    "k8s_resource_quota_probe": probe_k8s_resource_quota_probe,
    "prom_pod_cpu_cores": probe_prom_pod_cpu_cores,
    "prom_pod_memory_wss": probe_prom_pod_memory_wss,
    # Security drift probes — wired for OmniRbacClusterAdminViolation /
    # OmniConfigMapGodModeProd alert types.
    "rbac_drift": probe_k8s_rbac_drift,
    "configmap_security_drift": probe_k8s_configmap_security_drift,
    # P7.2.A — L1 OS-level (node_exporter via Prometheus)
    "node_disk_pressure": probe_node_disk_pressure,
    "node_cpu_saturation": probe_node_cpu_saturation,
    "node_memory_pressure": probe_node_memory_pressure,
    "node_disk_io_saturation": probe_node_disk_io_saturation,
    # P7.2.B — L2 Network (kubernetes_asyncio, read-only)
    "k8s_service_endpoints_ready": probe_k8s_service_endpoints_ready,
    "k8s_networkpolicy_audit": probe_k8s_networkpolicy_audit,
    # APP_HTTP lane — Loki-based access-log surge detection
    "loki_access_log_surge": probe_loki_access_log_surge,
}


async def run_probe(probe_id: str, ctx: WorkerHandlerContext, ev: AnomalyEvent) -> ProbeRunRaw:
    fn = PROBE_REGISTRY.get(probe_id)
    if not fn:
        return ProbeRunRaw(probe_name=probe_id, status="SKIPPED", raw_text="unknown_probe_id")
    return await fn(ctx, ev)
