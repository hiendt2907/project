"""Preflight: Redis learned map + có điều kiện vector infra_topology — trước clarification."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from rag.pgvector_store import COLLECTION_INFRA_TOPOLOGY
from workers.clarification import is_scope_ambiguous_cpu_ram

logger = logging.getLogger(__name__)

REDIS_LEARNED_BYNAME = "infra:learned:byname"


def _embedding_from_ollama(resp: dict[str, Any]) -> list[float]:
    if "embedding" in resp:
        emb = resp["embedding"]
        return list(emb) if not isinstance(emb, list) else emb
    embs = resp.get("embeddings")
    if isinstance(embs, list) and embs:
        return list(embs[0])
    raise ValueError("embed response missing embedding(s)")


@dataclass
class LearnedContext:
    namespace: str | None = None
    pod_name: str | None = None
    service_name: str | None = None
    matched_token: str | None = None
    infra_blocks: list[str] = field(default_factory=list)
    had_vector_search: bool = False
    clarification_bypass: bool = False
    embed_vector: list[float] | None = None  # tái dùng cho enrich (tránh embed 2 lần)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w.\-]+", (text or "").lower())


def _apply_bypass_heuristic(text: str, learned: LearnedContext) -> None:
    t = (text or "").lower()
    if learned.namespace and learned.namespace.lower() in t.replace("_", "-"):
        learned.clarification_bypass = True
    if learned.matched_token and learned.matched_token.lower() in t:
        learned.clarification_bypass = True
    if learned.pod_name and learned.pod_name.lower() in t:
        learned.clarification_bypass = True
    if learned.service_name and learned.service_name.lower() in t:
        learned.clarification_bypass = True


async def preflight_infra_kb(ctx: Any, user_text: str) -> LearnedContext:
    learned = LearnedContext()
    raw = (user_text or "").strip()
    if not raw:
        return learned

    toks = _tokens(raw)
    toks_sorted = sorted(set(toks), key=len, reverse=True)
    try:
        for tok in toks_sorted:
            if len(tok) < 2:
                continue
            raw_ns = await ctx.redis.hget(REDIS_LEARNED_BYNAME, tok)
            if isinstance(raw_ns, str) and raw_ns.strip():
                learned.namespace = raw_ns.strip()
                learned.matched_token = tok
                break
    except Exception as e:
        logger.debug("preflight redis learned: %s", e)

    if learned.namespace and learned.matched_token:
        learned.infra_blocks.append(
            f"[CONTEXT: learned_infra]\nentity_token={learned.matched_token} namespace={learned.namespace}"
        )

    # Tránh embed trước clarification cho "check CPU/RAM" mơ hồ (chưa có namespace từ map).
    skip_vector_early = is_scope_ambiguous_cpu_ram(raw) and not learned.namespace
    if skip_vector_early:
        _apply_bypass_heuristic(raw, learned)
        return learned

    if learned.namespace:
        try:
            topo = await ctx.redis.get("state:current_topology")
            if topo:
                learned.infra_blocks.append(f"[CONTEXT: topology_cache]\n{topo[:2400]}")
        except Exception as e:
            logger.debug("preflight topology_cache: %s", e)
        _apply_bypass_heuristic(raw, learned)
        return learned

    if len(raw) < 8:
        _apply_bypass_heuristic(raw, learned)
        return learned

    try:
        emb_resp = await ctx.ollama.embed(
            model=ctx.settings.embed_model,
            input=raw[:2000],
            keep_alive=ctx.settings.ollama_keep_alive,
        )
        vector = _embedding_from_ollama(emb_resp)
        learned.embed_vector = vector
        resp = await ctx.vector_store.query_points(
            collection_name=COLLECTION_INFRA_TOPOLOGY,
            query=vector,
            limit=3,
            score_threshold=0.52,
            with_payload=True,
        )
        learned.had_vector_search = True
        for i, pt in enumerate(resp.points or []):
            pl = dict(pt.payload or {})
            chunk = pl.get("text") or pl.get("summary") or ""
            if chunk:
                learned.infra_blocks.append(f"[CONTEXT: infra_topology score={pt.score:.3f}]\n{chunk[:1800]}")
            if i == 0 and pt.score >= 0.58:
                if pl.get("namespace"):
                    learned.namespace = str(pl.get("namespace"))
                if pl.get("pod_name"):
                    learned.pod_name = str(pl.get("pod_name"))
                if pl.get("service_name"):
                    learned.service_name = str(pl.get("service_name"))
    except Exception as e:
        logger.debug("preflight vector: %s", e)

    _apply_bypass_heuristic(raw, learned)
    return learned
