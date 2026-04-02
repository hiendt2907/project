"""Bổ sung ngữ cảnh từ Redis topology + Postgres infra_topology — trước LLM."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from rag.pgvector_store import COLLECTION_INFRA_TOPOLOGY

if TYPE_CHECKING:
    from workers.infra_preflight import LearnedContext

logger = logging.getLogger(__name__)

REDIS_LEARNED_META = "infra:learned:meta"


async def fetch_infra_injection_for_fallback(ctx: Any, user_text: str) -> str:
    """
    Bơm máu cho SRE Fallback: snapshot DeepScout (Redis) + snippet infra_topology (Postgres).
    Gọi trong slow_path khi đã giữ Ollama slot — không tự bịa số pod/ns.
    """
    parts: list[str] = []
    try:
        topo = await ctx.redis.get("state:current_topology")
        if topo:
            parts.append(f"[topology_cache / DeepScout Redis]\n{str(topo)[:3500]}")
    except Exception as e:
        logger.debug("fallback infra topology redis: %s", e)
    try:
        meta_raw = await ctx.redis.get(REDIS_LEARNED_META)
        if meta_raw:
            parts.append(f"[learned_meta / autonomous scout]\n{meta_raw[:1200]}")
    except Exception as e:
        logger.debug("fallback learned_meta: %s", e)
    raw = (user_text or "").strip()
    if len(raw) >= 8:
        try:
            emb_resp = await ctx.ollama.embed(
                model=ctx.settings.embed_model,
                input=raw[:2000],
                keep_alive=ctx.settings.ollama_keep_alive,
            )
            vector = _embedding_from_ollama(emb_resp)
            resp = await ctx.vector_store.query_points(
                collection_name=COLLECTION_INFRA_TOPOLOGY,
                query=vector,
                limit=2,
                score_threshold=0.52,
                with_payload=True,
            )
            for pt in resp.points or []:
                pl = dict(pt.payload or {})
                chunk = pl.get("text") or pl.get("summary") or ""
                if chunk:
                    parts.append(f"[infra_topology Postgres score={pt.score:.3f}]\n{str(chunk)[:1600]}")
        except Exception as e:
            logger.debug("fallback infra postgres: %s", e)
    if not parts:
        return ""
    return "\n\n".join(parts)


def _embedding_from_ollama(resp: dict[str, Any]) -> list[float]:
    if "embedding" in resp:
        emb = resp["embedding"]
        return list(emb) if not isinstance(emb, list) else emb
    embs = resp.get("embeddings")
    if isinstance(embs, list) and embs:
        return list(embs[0])
    raise ValueError("embed response missing embedding(s)")


async def enrich_working_text_with_infra(
    ctx: Any,
    user_text: str,
    learned: LearnedContext | None = None,
) -> str:
    """Ưu tiên cache + semantic infra; tái dùng kết quả preflight khi có."""
    raw = (user_text or "").strip()
    if len(raw) < 8:
        return raw

    if learned is not None and (learned.had_vector_search or learned.infra_blocks):
        blocks = list(learned.infra_blocks)
        try:
            topo = await ctx.redis.get("state:current_topology")
            if topo and not any("topology_cache" in x for x in blocks):
                blocks.insert(0, f"[CONTEXT: topology_cache]\n{topo[:2800]}")
        except Exception as e:
            logger.debug("infra redis topology: %s", e)
        hint = f"ns={learned.namespace or '-'} pod={learned.pod_name or '-'} svc={learned.service_name or '-'}"
        blocks.append(f"[CONTEXT: learned_infra]\n{hint}")
        return "\n\n".join(blocks) + f"\n\n[USER_MESSAGE]\n{raw}"

    blocks: list[str] = []
    try:
        topo = await ctx.redis.get("state:current_topology")
        if topo:
            blocks.append(f"[CONTEXT: topology_cache]\n{topo[:2800]}")
    except Exception as e:
        logger.debug("infra redis topology: %s", e)
    try:
        emb_resp = await ctx.ollama.embed(
            model=ctx.settings.embed_model,
            input=raw[:2000],
            keep_alive=ctx.settings.ollama_keep_alive,
        )
        vector = _embedding_from_ollama(emb_resp)
        resp = await ctx.vector_store.query_points(
            collection_name=COLLECTION_INFRA_TOPOLOGY,
            query=vector,
            limit=3,
            score_threshold=0.52,
            with_payload=True,
        )
        for pt in resp.points or []:
            pl = dict(pt.payload or {})
            chunk = pl.get("text") or pl.get("summary") or ""
            if chunk:
                blocks.append(f"[CONTEXT: infra_topology score={pt.score:.3f}]\n{chunk[:1800]}")
    except Exception as e:
        logger.debug("infra postgres: %s", e)
    if not blocks:
        return raw
    return "\n\n".join(blocks) + f"\n\n[USER_MESSAGE]\n{raw}"
