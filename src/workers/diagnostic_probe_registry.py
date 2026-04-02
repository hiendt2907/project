from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from kubernetes_asyncio import client

from workers.diagnostic_evidence import ProbeRunRaw
from workers.k8s_tools import _load_k8s_config
from workers.proactive_models import AnomalyEvent
from workers.handlers import WorkerHandlerContext

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


async def probe_redis_stream_len_inbound(ctx: WorkerHandlerContext, _ev: AnomalyEvent) -> ProbeRunRaw:
    key = ctx.settings.stream_inbound
    try:
        n = await ctx.redis.xlen(key)
        return ProbeRunRaw(
            probe_name="redis_stream_len_inbound",
            status="PASSED",
            raw_text=f"{key} XLEN={n}",
            structured_hint={"stream": key, "xlen": int(n)},
        )
    except Exception as e:
        return ProbeRunRaw(
            probe_name="redis_stream_len_inbound",
            status="FAILED",
            raw_text=str(e)[:2000],
        )


PROBE_REGISTRY: dict[str, ProbeFn] = {
    "redis_ping": probe_redis_ping,
    "k8s_list_pods_namespace": probe_k8s_list_pods_namespace,
    "redis_stream_len_inbound": probe_redis_stream_len_inbound,
}


async def run_probe(probe_id: str, ctx: WorkerHandlerContext, ev: AnomalyEvent) -> ProbeRunRaw:
    fn = PROBE_REGISTRY.get(probe_id)
    if not fn:
        return ProbeRunRaw(probe_name=probe_id, status="SKIPPED", raw_text="unknown_probe_id")
    return await fn(ctx, ev)
