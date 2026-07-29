"""Deep Scout — baseline hạ tầng: K8s + VM + Redis + Postgres RAG (không subprocess)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import psutil
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiException
from rag.pgvector_store import (
    COLLECTION_INFRA_TOPOLOGY, 
    PointStruct
)
from workers.settings import WorkerSettings

logger = logging.getLogger(__name__)

REDIS_KEY_HOST = "sys:host:specs"
REDIS_KEY_BASELINE = "metrics:baseline:24h"
REDIS_TTL_SEC = 3600
_REDIS_WRITE_MAX_ATTEMPTS = 3
_REDIS_WRITE_BACKOFF_SEC = 0.5


async def _retry_redis_write(coro_factory, *, max_attempts: int = _REDIS_WRITE_MAX_ATTEMPTS) -> None:
    """Retry có giới hạn cho ghi Redis (timeout/connection thoáng qua tự phục hồi).
    Raise lỗi cuối cùng nếu hết attempt — caller escalate qua ErrorLedger, KHÔNG nuốt
    im lặng sau khi retry hết (khác hành vi cũ: log warning rồi bỏ qua)."""
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            await coro_factory()
            return
        except Exception as e:  # noqa: BLE001 — retry mọi lỗi Redis thoáng qua, escalate ở caller
            last_exc = e
            if attempt < max_attempts:
                await asyncio.sleep(_REDIS_WRITE_BACKOFF_SEC * attempt)
    if last_exc is not None:
        raise last_exc

# Khóa ConfigMap — substring (lower); tránh "key" đơn độc (quá rộng)
_SENSITIVE_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "auth",
    "bearer",
    "apikey",
    "api_key",
    "privatekey",
    "access_key",
    "client_secret",
)


def _is_sensitive_config_key(name: str) -> bool:
    n = (name or "").lower()
    if n.startswith("kubernetes.io/"):
        return True
    return any(m in n for m in _SENSITIVE_KEY_MARKERS)


def _redact_configmap_entries(data: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (data or {}).items():
        if _is_sensitive_config_key(k):
            out[k] = "<REDACTED>"
        else:
            out[k] = (v or "")[:500]
    return out


async def _kube_load() -> None:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        await config.load_kube_config()


def _embedding_from_response(resp: dict[str, Any]) -> list[float]:
    if "embedding" in resp:
        emb = resp["embedding"]
        return list(emb) if not isinstance(emb, list) else emb
    embs = resp.get("embeddings")
    if isinstance(embs, list) and embs:
        return list(embs[0])
    raise ValueError("embed response missing embedding(s)")


@dataclass
class DeepScoutSummary:
    n_nodes: int = 0
    n_pods: int = 0
    n_services: int = 0
    vm_url: str = ""
    errors: list[str] = field(default_factory=list)


async def _layer_host_node(ws: WorkerSettings) -> tuple[dict[str, Any], str]:
    """psutil (pod) + K8s nodes."""
    lines: list[str] = []
    host: dict[str, Any] = {"note": "Trong pod Linux — không phải nhiệt độ chip M4 host."}
    try:
        host["cpu_count_logical"] = psutil.cpu_count(logical=True)
        host["cpu_count_physical"] = psutil.cpu_count(logical=False)
        vm = psutil.virtual_memory()
        host["ram_total_gib"] = round(vm.total / (1024**3), 2)
        host["ram_percent"] = vm.percent
        disk = psutil.disk_usage("/")
        host["disk_root_percent"] = disk.percent
        io = psutil.disk_io_counters()
        if io:
            host["disk_read_bytes"] = io.read_bytes
            host["disk_write_bytes"] = io.write_bytes
        lines.append(
            f"Host(pod): CPU logical={host.get('cpu_count_logical')} "
            f"RAM {host.get('ram_total_gib')}GiB ~{host.get('ram_percent')}% "
            f"disk / {host.get('disk_root_percent')}%."
        )
    except Exception as e:
        host["psutil_error"] = str(e)
        lines.append(f"psutil: {e!s}")

    nodes: list[dict[str, Any]] = []
    try:
        await _kube_load()
        v1 = client.CoreV1Api()
        try:
            nl = await v1.list_node()
            for n in nl.items or []:
                nodes.append(
                    {
                        "name": n.metadata.name,
                        "capacity_cpu": str(n.status.capacity.get("cpu", "") if n.status and n.status.capacity else ""),
                        "capacity_mem": str(
                            n.status.capacity.get("memory", "") if n.status and n.status.capacity else ""
                        ),
                    }
                )
            lines.append(f"K8s nodes={len(nodes)}: " + ", ".join(x["name"] for x in nodes[:8]))
        finally:
            await v1.api_client.close()
    except Exception as e:
        host["k8s_node_error"] = str(e)
        lines.append(f"list_node: {e!s}")

    host["nodes"] = nodes
    return host, "\n".join(lines)


async def _layer_network_topology() -> tuple[dict[str, Any], str]:
    """Service → ClusterIP → ports → endpoint pod IPs."""
    topo: dict[str, Any] = {"services": []}
    lines: list[str] = []
    try:
        await _kube_load()
        v1 = client.CoreV1Api()
        net = client.NetworkingV1Api()
        try:
            svcs = await v1.list_service_for_all_namespaces(limit=500)
            eps = await v1.list_endpoints_for_all_namespaces(limit=500)
            ep_index: dict[tuple[str, str], Any] = {}
            for e in eps.items or []:
                ns = e.metadata.namespace or ""
                name = e.metadata.name or ""
                ep_index[(ns, name)] = e
            for s in svcs.items or []:
                ns = s.metadata.namespace or ""
                name = s.metadata.name or ""
                spec = s.spec
                cluster_ip = getattr(spec, "cluster_ip", None) or ""
                ports_raw = []
                for p in spec.ports or []:
                    ports_raw.append(
                        {"port": p.port, "protocol": p.protocol, "name": p.name or ""}
                    )
                ep = ep_index.get((ns, name))
                pod_ips: list[str] = []
                if ep and ep.subsets:
                    for sub in ep.subsets:
                        for a in sub.addresses or []:
                            if a.ip:
                                pod_ips.append(a.ip)
                            if a.target_ref and a.target_ref.kind == "Pod" and a.target_ref.name:
                                pod_ips.append(f"pod:{a.target_ref.name}")
                entry = {
                    "namespace": ns,
                    "name": name,
                    "type": spec.type or "",
                    "cluster_ip": cluster_ip,
                    "ports": ports_raw,
                    "endpoint_hints": pod_ips[:20],
                }
                topo["services"].append(entry)
                if len(lines) < 40:
                    lines.append(f"{ns}/{name} {cluster_ip} ports={[p['port'] for p in ports_raw]}")
            try:
                ing = await net.list_ingress_for_all_namespaces(limit=200)
                topo["ingress"] = []
                for i in ing.items or []:
                    rules = []
                    for r in i.spec.rules or []:
                        h = r.host or ""
                        paths = [p.path for p in (r.http.paths or [])]
                        rules.append({"host": h, "paths": paths[:5]})
                    topo["ingress"].append(
                        {"namespace": i.metadata.namespace, "name": i.metadata.name, "rules": rules[:5]}
                    )
                lines.append(f"Ingress count={len(topo['ingress'])}")
            except Exception as e:
                topo["ingress_error"] = str(e)
        finally:
            await v1.api_client.close()
            await net.api_client.close()
    except Exception as e:
        topo["error"] = str(e)
        lines.append(f"topology: {e!s}")
    return topo, "\n".join(lines) if lines else "(no services)"


async def _layer_metrics_baseline(ws: WorkerSettings) -> tuple[dict[str, Any], str]:
    base = ws.prometheus_url.strip().rstrip("/")
    out: dict[str, Any] = {"prometheus_url": base, "queries": {}}
    lines: list[str] = [f"Prometheus URL={base}"]
    queries = [
        ("count_up", "count(up)"),
        ("node_idle_sample", 'avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))'),
    ]
    try:
        async with httpx.AsyncClient(timeout=30.0) as hc:
            for label, promql in queries:
                try:
                    r = await hc.get(f"{base}/api/v1/query", params={"query": promql})
                    r.raise_for_status()
                    data = r.json()
                    if data.get("status") == "success":
                        res = (data.get("data") or {}).get("result") or []
                        if res:
                            v = res[0].get("value")
                            out["queries"][label] = v[1] if v and len(v) > 1 else res[0]
                        else:
                            out["queries"][label] = None
                    else:
                        out["queries"][label] = f"err:{data.get('error', '')}"
                except Exception as e:
                    out["queries"][label] = f"exc:{e!s}"
            lines.append(f"baseline samples: {json.dumps(out['queries'], ensure_ascii=False)[:400]}")
    except Exception as e:
        out["error"] = str(e)
        lines.append(f"Prometheus baseline: {e!s}")
    return out, "\n".join(lines)


async def _layer_cluster_state(ws: WorkerSettings) -> tuple[dict[str, Any], str]:
    state: dict[str, Any] = {}
    lines: list[str] = []
    ns_allow = [x.strip() for x in ws.deep_scout_configmap_namespaces.split(",") if x.strip()]
    try:
        await _kube_load()
        v1 = client.CoreV1Api()
        try:
            pods = await v1.list_pod_for_all_namespaces(limit=500)
            state["pod_count"] = len(pods.items or [])
            by_ns: dict[str, int] = {}
            for p in pods.items or []:
                n = p.metadata.namespace or "?"
                by_ns[n] = by_ns.get(n, 0) + 1
            state["pods_by_namespace"] = dict(sorted(by_ns.items(), key=lambda x: -x[1])[:30])
            lines.append(f"Pods total={state['pod_count']}")

            try:
                pvs = await v1.list_persistent_volume(limit=200)
                state["pv_count"] = len(pvs.items or [])
                lines.append(f"PV count={state['pv_count']}")
            except Exception as e:
                state["pv_error"] = str(e)

            try:
                pvcs = await v1.list_persistent_volume_claim_for_all_namespaces(limit=300)
                state["pvc_count"] = len(pvcs.items or [])
                lines.append(f"PVC count={state['pvc_count']}")
            except Exception as e:
                state["pvc_error"] = str(e)

            cm_summaries: list[dict[str, Any]] = []
            for ns in ns_allow:
                try:
                    cms = await v1.list_namespaced_config_map(namespace=ns, limit=100)
                    for cm in cms.items or []:
                        name = cm.metadata.name or ""
                        raw_data = dict(cm.data or {})
                        red = _redact_configmap_entries(raw_data)
                        cm_summaries.append(
                            {
                                "namespace": ns,
                                "name": name,
                                "keys": list(red.keys())[:40],
                                "redacted_values_preview": {k: v[:80] for k, v in list(red.items())[:5]},
                            }
                        )
                except ApiException as e:
                    cm_summaries.append({"namespace": ns, "error": f"{e.status}:{e.reason}"})
            state["configmaps_summary"] = cm_summaries[:50]
            lines.append(f"ConfigMaps scanned ns={ns_allow} entries={len(cm_summaries)}")
        finally:
            await v1.api_client.close()
    except Exception as e:
        state["error"] = str(e)
        lines.append(f"cluster state: {e!s}")
    return state, "\n".join(lines)


async def _embed_and_upsert(
    llm: VLLMClient,
    ws: WorkerSettings,
    vector_store: Any,
    chunks: list[tuple[str, str, dict[str, Any]]],
    sem: asyncio.Semaphore,
) -> None:
    # Index/schema do RedisVectorStore.ensure_ready() tạo (pgvector đã gỡ 2026).
    pass

    async def one(cid: str, text: str, payload: dict[str, Any]) -> None:
        async with sem:
            try:
                resp = await llm.embed(
                    model=ws.embed_model,
                    input=text[:8000],
                )
                vec = _embedding_from_response(resp)
            except Exception as e:
                logger.warning("deep_scout embed fail %s: %s", cid, e)
                return
        try:
            await vector_store.upsert(
                collection_name=COLLECTION_INFRA_TOPOLOGY,
                points=[PointStruct(id=cid, vector=vec, payload=payload)],
            )
        except Exception as e:
            logger.warning("deep_scout pgvector upsert %s: %s", cid, e)

    await asyncio.gather(*[one(cid, text, pay) for cid, text, pay in chunks])


def _point_id_stable(s: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "infra_topology:" + s))


async def run_deep_scout(ctx: Any, *, periodic: bool = False) -> DeepScoutSummary:
    """Quét baseline — lưu Redis + RAG infra_topology."""
    summary = DeepScoutSummary(vm_url=ctx.settings.prometheus_url)
    ws: WorkerSettings = ctx.settings
    r = ctx.redis
    llm: VLLMClient = ctx.llm
    vector_store = ctx.vector_store

    host_d, host_txt = await _layer_host_node(ws)
    topo_d, topo_txt = await _layer_network_topology()
    met_d, met_txt = await _layer_metrics_baseline(ws)
    st_d, st_txt = await _layer_cluster_state(ws)

    summary.n_nodes = len(host_d.get("nodes") or [])
    summary.n_pods = int(st_d.get("pod_count") or 0)
    summary.n_services = len(topo_d.get("services") or [])

    try:
        await _retry_redis_write(
            lambda: r.set(REDIS_KEY_HOST, json.dumps(host_d, ensure_ascii=False), ex=REDIS_TTL_SEC)
        )
        # Topology không còn lưu Redis — RAG infra_topology + SRE_KNOWLEDGE (pgvector).
        await _retry_redis_write(
            lambda: r.set(REDIS_KEY_BASELINE, json.dumps(met_d, ensure_ascii=False), ex=REDIS_TTL_SEC)
        )
    except Exception as e:
        summary.errors.append(f"redis:{e!s}")
        logger.warning("deep_scout redis: retry exhausted, escalating: %s", e)
        ledger = getattr(ctx, "ledger", None)
        if ledger is not None:
            try:
                await ledger.record_exception(
                    e, phase="init", component="deep_scout_redis_write", swallow_errors=True,
                )
            except Exception:  # noqa: BLE001 — ledger tự nó không được phép làm crash deep_scout
                logger.exception("deep_scout: escalate to error ledger cũng fail")

    chunks: list[tuple[str, str, dict[str, Any]]] = [
        (
            _point_id_stable("host_node"),
            f"DeepScout Host/Node baseline.\n{host_txt}\nNodes JSON keys: cpu, ram, disk.",
            {"kind": "host_node", "text": host_txt[:4000], "periodic": periodic},
        ),
        (
            _point_id_stable("network_topology"),
            f"K8s Service topology (ClusterIP → endpoints).\n{topo_txt}",
            {"kind": "topology", "text": topo_txt[:4000], "periodic": periodic},
        ),
        (
            _point_id_stable("metrics_baseline"),
            f"Prometheus baseline 24h window samples.\n{met_txt}",
            {"kind": "metrics", "text": met_txt[:4000], "periodic": periodic},
        ),
        (
            _point_id_stable("cluster_state"),
            f"Cluster state: pods, PV, PVC, ConfigMaps (redacted).\n{st_txt}",
            {"kind": "cluster", "text": st_txt[:4000], "periodic": periodic},
        ),
    ]

    sem = asyncio.Semaphore(ws.deep_scout_embed_concurrency)
    try:
        await _embed_and_upsert(llm, ws, vector_store, chunks, sem)
    except Exception as e:
        summary.errors.append(f"rag_embed:{e!s}")
        logger.exception("deep_scout embed pipeline")

    if not periodic:
        logger.info(
            "deep_scout done nodes=%s pods=%s services=%s",
            summary.n_nodes,
            summary.n_pods,
            summary.n_services,
        )
    return summary


async def deep_scout_periodic_loop(ctx: Any, stop: asyncio.Event) -> None:
    ws: WorkerSettings = ctx.settings
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=float(ws.deep_scout_interval_sec))
            return
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            return
        try:
            await run_deep_scout(ctx, periodic=True)
        except Exception:
            logger.exception("deep_scout periodic run failed")
        try:
            from init import deep_scout_autonomous as _dsa

            await _dsa.run_deep_scout_autonomous(ctx, periodic=True)
        except Exception:
            logger.exception("deep_scout_autonomous periodic run failed")
