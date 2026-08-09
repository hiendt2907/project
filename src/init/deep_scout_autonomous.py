"""Deep Scout tự học — ingest K8s+VM, tóm tắt Qwen 1.5B, Postgres RAG (không subprocess)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from llm.factory import build_llm_client
from kubernetes_asyncio import client, config
from rag.pgvector_store import (
    COLLECTION_INFRA_TOPOLOGY, 
    EMBED_DIM, 
    PointStruct
)
from workers.settings import WorkerSettings

logger = logging.getLogger(__name__)

SYNTH_SYSTEM_VI = (
    "Mày là một Senior SRE. Hãy đọc JSON này và viết một đoạn tóm tắt kỹ thuật dài 3 câu. "
    "Phải bao gồm: Chức năng của nó, nó thuộc về ai (Namespace), nó kết nối tới đâu (Network), "
    "và tình trạng sức khỏe trung bình (Baseline). Tuyệt đối không nói nhảm."
)

BIGBANG_SYNTH_SYSTEM_VI = (
    "Mày là Senior SRE. Đọc fragment JSON kubectl (cluster dump). Viết 'bí kíp vận hành' dày đặc: "
    "topology ngầm, baseline tài nguyên, rủi ro Events/Ingress/PVC/ConfigMap. Tối đa ~1200 ký tự, không markdown fence."
)


@dataclass
class AutonomousScoutSummary:
    pods_processed: int = 0
    services_processed: int = 0
    bigbang_chunks: int = 0
    synth_cached: int = 0  # entity không đổi ⇒ bỏ qua cả LLM synth lẫn embed
    synth_called: int = 0  # entity mới/đã đổi/quá hạn refresh ⇒ có gọi LLM
    errors: list[str] = field(default_factory=list)


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


def _point_id_autonomous(kind: str, ns: str, name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"autonomous_scout:{kind}:{ns}:{name}"))


async def _vm_namespace_baselines(ws: WorkerSettings) -> tuple[bool, dict[str, Any]]:
    """Trả về (series_ok, dict gồm cpu/mem hints theo namespace nếu có)."""
    base = ws.prometheus_url.strip().rstrip("/")
    out: dict[str, Any] = {"namespaces_cpu": {}, "namespaces_mem": {}, "raw_errors": []}
    ok = False
    queries = [
        (
            "probe",
            "count(container_cpu_usage_seconds_total)",
        ),
        (
            "cpu_ns",
            'topk(20, sum by (namespace) (rate(container_cpu_usage_seconds_total{container!="POD",container!=""}[5m])))',
        ),
        (
            "mem_ns",
            'topk(20, sum by (namespace) (avg_over_time(container_memory_working_set_bytes{container!="POD",container!=""}[5m])))',
        ),
    ]
    try:
        async with httpx.AsyncClient(timeout=25.0) as hc:
            for label, promql in queries:
                try:
                    r = await hc.get(f"{base}/api/v1/query", params={"query": promql})
                    r.raise_for_status()
                    data = r.json()
                    if data.get("status") != "success":
                        out["raw_errors"].append(f"{label}:{data.get('error', '')}")
                        continue
                    res = (data.get("data") or {}).get("result") or []
                    if label == "probe" and res:
                        v = res[0].get("value")
                        cnt = float(v[1]) if v and len(v) > 1 else 0.0
                        if cnt > 0:
                            ok = True
                    elif label == "cpu_ns":
                        for it in res:
                            m = it.get("metric") or {}
                            ns = m.get("namespace") or m.get("kubernetes_namespace") or ""
                            v = it.get("value")
                            if ns and v and len(v) > 1:
                                out["namespaces_cpu"][ns] = v[1]
                    elif label == "mem_ns":
                        for it in res:
                            m = it.get("metric") or {}
                            ns = m.get("namespace") or m.get("kubernetes_namespace") or ""
                            v = it.get("value")
                            if ns and v and len(v) > 1:
                                out["namespaces_mem"][ns] = v[1]
                except Exception as e:
                    out["raw_errors"].append(f"{label}:exc:{e!s}")
    except Exception as e:
        out["raw_errors"].append(str(e))
    return ok, out


def _pod_ports(p: Any) -> list[dict[str, Any]]:
    ports: list[dict[str, Any]] = []
    for c in p.spec.containers or []:
        for pt in c.ports or []:
            ports.append(
                {
                    "container": c.name,
                    "port": pt.container_port,
                    "name": pt.name or "",
                    "protocol": pt.protocol or "TCP",
                }
            )
    return ports[:24]


def _pod_containers(p: Any) -> list[dict[str, str]]:
    return [{"name": c.name or "", "image": (c.image or "")[:120]} for c in (p.spec.containers or [])][:12]


# Trường KHÔNG được đưa vào fingerprint: chúng đổi mỗi vòng quét dù hạ tầng
# đứng yên, nên nếu tính vào hash thì dedup vô tác dụng — mọi entity luôn "đổi".
# Vẫn truyền vào prompt để bản synth có số liệu baseline khi thật sự phải viết lại.
_VOLATILE_ENTITY_FIELDS = ("namespace_cpu_rate_sample", "namespace_mem_sample")

_SYNTH_CACHE_PREFIX = "omni:scout:synth:"


def _entity_fingerprint(entity_json: dict[str, Any]) -> str:
    """Hash phần CẤU TRÚC của entity (bỏ số đo biến thiên).

    Deep scout trước đây gọi LLM cho MỌI pod/service ở MỌI vòng, kể cả khi không
    có gì đổi: 93 entity × ~9.5s tuần tự (Ollama num_parallel=1) ≈ 15 phút LLM
    liên tục mỗi chu kỳ 30 phút, và tranh hàng đợi với advisory thật.
    """
    stable = {k: v for k, v in entity_json.items() if k not in _VOLATILE_ENTITY_FIELDS}
    canonical = json.dumps(stable, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()


async def _cached_summary(redis: Any, point_id: str, fingerprint: str, max_age_sec: int) -> str | None:
    """Bản tóm tắt còn dùng được, hoặc None nếu phải synth lại.

    Trả None khi: không có Redis, chưa từng cache, entity đã đổi, hoặc bản cache
    quá cũ (`autonomous_synth_refresh_sec`) — mốc tuổi giữ baseline khỏi đóng băng
    vĩnh viễn ở lần quét đầu tiên.
    """
    if redis is None:
        return None
    try:
        raw = await redis.get(f"{_SYNTH_CACHE_PREFIX}{point_id}")
    except Exception as e:
        logger.warning("autonomous synth cache read %s: %s", point_id, e)
        return None
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if rec.get("fingerprint") != fingerprint:
        return None
    if (time.time() - float(rec.get("ts") or 0.0)) > max_age_sec:
        return None
    text = rec.get("summary")
    return text if isinstance(text, str) and text else None


async def _store_summary(redis: Any, point_id: str, fingerprint: str, summary: str) -> None:
    if redis is None or not summary:
        return
    try:
        await redis.set(
            f"{_SYNTH_CACHE_PREFIX}{point_id}",
            json.dumps({"fingerprint": fingerprint, "summary": summary, "ts": time.time()}),
            ex=_SYNTH_CACHE_TTL_SEC,
        )
    except Exception as e:
        logger.warning("autonomous synth cache write %s: %s", point_id, e)


# TTL rộng hơn hẳn refresh window — cache hết hạn chỉ là mất dedup, không sai dữ liệu.
_SYNTH_CACHE_TTL_SEC = 30 * 24 * 3600


async def _synthesize_one(
    llm: VLLMClient,
    ws: WorkerSettings,
    entity_json: dict[str, Any],
    sem: asyncio.Semaphore,
) -> str:
    payload = json.dumps(entity_json, ensure_ascii=False)[:12000]
    user = f"JSON:\n{payload}"
    async with sem:
        try:
            resp = await llm.chat(
                model=ws.model_helper,
                messages=[
                    {"role": "system", "content": SYNTH_SYSTEM_VI},
                    {"role": "user", "content": user},
                ],
            )
            return ((resp.get("message") or {}).get("content") or "").strip()[:4000]
        except Exception as e:
            logger.warning("autonomous_synth fail: %s", e)
            return f"(synthesis_error: {e!s})"


async def _embed_upsert_one(
    llm: VLLMClient,
    ws: WorkerSettings,
    vector_store: Any,
    *,
    point_id: str,
    summary: str,
    payload: dict[str, Any],
) -> None:
    try:
        resp = await llm.embed(
            model=ws.embed_model,
            input=summary[:8000],
        )
        vec = _embedding_from_response(resp)
        if len(vec) != EMBED_DIM:
            vec = (vec + [0.0] * EMBED_DIM)[:EMBED_DIM]
    except Exception as e:
        logger.warning("autonomous embed fail %s: %s", point_id, e)
        return
    try:
        await vector_store.upsert(
            collection_name=COLLECTION_INFRA_TOPOLOGY,
            points=[PointStruct(id=point_id, vector=vec, payload=payload)],
        )
    except Exception as e:
        logger.warning("autonomous rag_upsert %s: %s", point_id, e)


_CHUNK = 115_000


async def _synthesize_bigbang_chunk(
    ws: WorkerSettings,
    llm: VLLMClient,
    chunk: str,
    idx: int,
    cluster_digest: dict[str, Any],
    sem: asyncio.Semaphore,
) -> str:
    digest_s = json.dumps(cluster_digest, ensure_ascii=False)[:2500]
    user = f"[cluster_digest]\n{digest_s}\n\n[kubectl_json_fragment]\n{chunk}"
    backend = (ws.scout_synth_backend or "ollama").strip().lower()
    async with sem:
        if backend == "gemini":
            from llm.gemini_client import gemini_generate_with_llm_fallback

            return (
                await gemini_generate_with_llm_fallback(
                    settings=ws,
                    llm=llm,
                    system_instruction=BIGBANG_SYNTH_SYSTEM_VI,
                    user_text=user[:1_050_000],
                    trace_id=f"bigbang-{idx}",
                    llm_model=ws.model_helper,
                )
            ).strip()[:4000]
        resp = await llm.chat(
            model=ws.model_helper,
            messages=[
                {"role": "system", "content": BIGBANG_SYNTH_SYSTEM_VI},
                {"role": "user", "content": user[:12000]},
            ],
        )
        return ((resp.get("message") or {}).get("content") or "").strip()[:4000]


async def _run_bigbang_cluster_ingest(
    llm: VLLMClient,
    periodic: bool,
    sem: asyncio.Semaphore,
    cluster_digest: dict[str, Any],
    vector_store: Any,
) -> int:
    ws: WorkerSettings = ctx.settings
    max_b = int(ws.autonomous_bigbang_max_json_mb) * 1024 * 1024
    logger.info(
        "[LAB_MODE] Unchained. Ingesting cluster via kubectl+json bigbang (max %s MB)",
        ws.autonomous_bigbang_max_json_mb,
    )
    proc = await asyncio.create_subprocess_exec(
        "kubectl",
        "get",
        "all,nodes,events,ingress,pvc,configmaps",
        "-A",
        "-o",
        "json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=max_b + 1,
    )
    raw_b, err_b = await proc.communicate()
    if proc.returncode != 0:
        logger.warning(
            "bigbang kubectl failed rc=%s err=%s",
            proc.returncode,
            (err_b or b"").decode("utf-8", errors="replace")[:500],
        )
        return 0
    text = (raw_b or b"").decode("utf-8", errors="replace")
    if not text.strip():
        return 0
    chunks: list[str] = []
    for i in range(0, len(text), _CHUNK):
        chunks.append(text[i : i + _CHUNK])
    n_done = 0
    for idx, ch in enumerate(chunks):
        try:
            summary_text = await _synthesize_bigbang_chunk(ws, llm, ch, idx, cluster_digest, sem)
            if not summary_text.strip():
                continue
            h = hashlib.sha256(ch.encode("utf-8", errors="replace")).hexdigest()[:24]
            pid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"bigbang:{idx}:{h}"))
            pay = {
                "entity_type": "cluster_bigbang",
                "namespace": "*",
                "pod_name": f"chunk_{idx}",
                "pod_ip": "",
                "ports": "[]",
                "text": summary_text[:4000],
                "summary": summary_text[:4000],
                "source": "autonomous_bigbang",
                "periodic": periodic,
                "chunk_index": idx,
                "ingest_secrets_raw": ws.ingest_secrets_raw,
            }
            await _embed_upsert_one(llm, ws, vector_store, point_id=pid, summary=summary_text, payload=pay)
            n_done += 1
        except Exception as e:
            logger.warning("bigbang chunk %s: %s", idx, e)
    logger.info("deep_scout bigbang done chunks=%s/%s", n_done, len(chunks))
    return n_done


async def run_deep_scout_autonomous(ctx: Any, *, periodic: bool = False) -> AutonomousScoutSummary:
    summary = AutonomousScoutSummary()
    ws: WorkerSettings = ctx.settings
    vector_store = ctx.vector_store

    local_llm = build_llm_client(base_url=ws.vllm_base_url, embed_url=ws.vllm_embed_url, timeout_s=float(ws.llm_chat_timeout_sec))
    try:
        llm = local_llm
        return await _run_deep_scout_autonomous_body(
            ctx=ctx, periodic=periodic, summary=summary, ws=ws, vector_store=vector_store, llm=llm
        )
    finally:
        await local_llm.aclose()


async def _run_deep_scout_autonomous_body(
    *,
    ctx: Any,
    periodic: bool,
    summary: AutonomousScoutSummary,
    ws: WorkerSettings,
    vector_store: Any,
    llm: VLLMClient,
) -> AutonomousScoutSummary:
    redis = getattr(ctx, "redis", None)
    refresh_sec = int(ws.autonomous_synth_refresh_sec)

    async def _summary_for(entity: dict[str, Any], point_id: str) -> tuple[str, bool]:
        """(text, đã_gọi_LLM). Cache hit ⇒ bỏ qua luôn embed+upsert ở call site."""
        fp = _entity_fingerprint(entity)
        cached = await _cached_summary(redis, point_id, fp, refresh_sec)
        if cached is not None:
            summary.synth_cached += 1
            return cached, False
        text = await _synthesize_one(llm, ws, entity, sem)
        summary.synth_called += 1
        await _store_summary(redis, point_id, fp, text)
        return text, True

    sem = asyncio.Semaphore(ws.autonomous_synth_concurrency)
    series_ok, vm_ns = await _vm_namespace_baselines(ws)
    if not series_ok:
        vm_ns["metrics_unavailable"] = True

    pods_raw: list[Any] = []
    svcs_raw: list[Any] = []
    nodes_raw: list[Any] = []
    ingress_raw: list[Any] = []

    try:
        await _kube_load()
        v1 = client.CoreV1Api()
        net = client.NetworkingV1Api()
        try:
            pl = await v1.list_pod_for_all_namespaces(limit=ws.autonomous_scout_max_pods)
            pods_raw = list(pl.items or [])
            sl = await v1.list_service_for_all_namespaces(limit=ws.autonomous_scout_max_services)
            svcs_raw = list(sl.items or [])
            nl = await v1.list_node()
            nodes_raw = list(nl.items or [])
            try:
                il = await net.list_ingress_for_all_namespaces(limit=200)
                ingress_raw = list(il.items or [])
            except Exception as e:
                summary.errors.append(f"ingress:{e!s}")
        finally:
            await v1.api_client.close()
            await net.api_client.close()
    except Exception as e:
        summary.errors.append(f"k8s:{e!s}")
        logger.exception("autonomous k8s list")
        return summary

    svc_by_ns: dict[str, list[str]] = {}
    for s in svcs_raw:
        ns = s.metadata.namespace or ""
        name = s.metadata.name or ""
        if not ns or not name:
            continue
        svc_by_ns.setdefault(ns, []).append(name)

    # `cluster_digest` (nodes/pods_total/vm_*) từng được nhét vào payload của TỪNG
    # entity. Nó đổi mỗi vòng quét dù pod/service đứng yên, nên mọi entity luôn
    # trông như "đã thay đổi" — dedup bên dưới sẽ vô tác dụng nếu giữ lại. Số liệu
    # cấp cụm vẫn còn ở `_run_bigbang_cluster_ingest`, nơi nó thực sự thuộc về.

    # Index/schema do RedisVectorStore.ensure_ready() tạo (pgvector đã gỡ 2026).

    for p in pods_raw:
        ns = p.metadata.namespace or ""
        pn = p.metadata.name or ""
        if not ns or not pn:
            continue
        ip = (p.status.pod_ip or "") if p.status else ""
        ports = _pod_ports(p)
        containers = _pod_containers(p)
        entity = {
            "entity_type": "pod",
            "namespace": ns,
            "name": pn,
            "pod_ip": ip,
            "ports": ports,
            "containers": containers,
            "services_same_namespace": svc_by_ns.get(ns, [])[:20],
            "namespace_cpu_rate_sample": (vm_ns.get("namespaces_cpu") or {}).get(ns),
            "namespace_mem_sample": (vm_ns.get("namespaces_mem") or {}).get(ns),
        }
        pid = _point_id_autonomous("pod", ns, pn)
        text, did_synth = await _summary_for(entity, pid)
        if not did_synth:
            summary.pods_processed += 1
            continue
        pay = {
            "entity_type": "pod",
            "pod_name": pn,
            "namespace": ns,
            "pod_ip": ip,
            "ports": json.dumps(ports, ensure_ascii=False)[:2000],
            "text": text[:4000],
            "summary": text[:4000],
            "source": "autonomous_scout",
            "periodic": periodic,
        }
        await _embed_upsert_one(llm, ws, vector_store, point_id=pid, summary=text, payload=pay)
        summary.pods_processed += 1

    svc_seen: set[tuple[str, str]] = set()
    for s in svcs_raw:
        ns = s.metadata.namespace or ""
        sn = s.metadata.name or ""
        if not ns or not sn:
            continue
        if (ns, sn) in svc_seen:
            continue
        svc_seen.add((ns, sn))
        spec = s.spec
        cluster_ip = getattr(spec, "cluster_ip", None) or ""
        prts = [{"port": x.port, "name": x.name or ""} for x in (spec.ports or [])][:12]
        entity = {
            "entity_type": "service",
            "namespace": ns,
            "name": sn,
            "cluster_ip": cluster_ip,
            "type": spec.type or "",
            "ports": prts,
            "namespace_cpu_rate_sample": (vm_ns.get("namespaces_cpu") or {}).get(ns),
            "namespace_mem_sample": (vm_ns.get("namespaces_mem") or {}).get(ns),
        }
        pid = _point_id_autonomous("svc", ns, sn)
        text, did_synth = await _summary_for(entity, pid)
        if not did_synth:
            summary.services_processed += 1
            continue
        pay = {
            "entity_type": "service",
            "service_name": sn,
            "namespace": ns,
            "pod_ip": "",
            "ports": json.dumps(prts, ensure_ascii=False)[:2000],
            "text": text[:4000],
            "summary": text[:4000],
            "source": "autonomous_scout",
            "periodic": periodic,
        }
        await _embed_upsert_one(llm, ws, vector_store, point_id=pid, summary=text, payload=pay)
        summary.services_processed += 1

    if ws.autonomous_probe_enabled and ws.opensandbox_enabled:
        try:
            from execution.manager import SandboxManager

            m = SandboxManager(ws)
            ok, msg = await m.health_check()
            if ok:
                await m.execute_shell(
                    kafka=getattr(ctx, "kafka", None),
                    command="echo autonomous_scout_probe_ok",
                    session_id="autonomous_scout",
                    trace_id="autonomous_scout",
                    image=ws.opensandbox_default_image,
                )
            else:
                logger.info("autonomous probe skipped: %s", msg)
        except Exception as e:
            summary.errors.append(f"probe:{e!s}")

    # Log CẢ vòng periodic: trước đây nhánh periodic im lặng nên không ai thấy nó
    # đang đốt LLM. synth_called/synth_cached là số để phát hiện dedup hỏng.
    logger.info(
        "deep_scout_autonomous done pods=%s services=%s synth_called=%s synth_cached=%s errors=%s periodic=%s",
        summary.pods_processed,
        summary.services_processed,
        summary.synth_called,
        summary.synth_cached,
        len(summary.errors),
        periodic,
    )
    return summary
