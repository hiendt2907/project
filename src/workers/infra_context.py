"""Bổ sung ngữ cảnh từ vector (k8s_expert collection + infra_topology) — không Redis topology/learned."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from rag.pgvector_store import COLLECTION_INFRA_TOPOLOGY, COLLECTION_K8S_EXPERT

if TYPE_CHECKING:
    from workers.infra_preflight import LearnedContext

logger = logging.getLogger(__name__)


def _expert_collection(ctx: Any) -> str:
    v = getattr(getattr(ctx, "settings", None), "pgvector_collection_k8s_expert", None)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return COLLECTION_K8S_EXPERT


async def fetch_k8s_expert_context_for_diagnostic(ctx: Any, query_text: str) -> str:
    """
    Trích đoạn semantic từ collection expert (kubernetes.io ingest) cho **luồng diagnostic sanitized**.
    Một embed + search; không thay SDK/probe — chỉ bổ sung tài liệu chính thức.
    """
    ws = getattr(ctx, "settings", None)
    if ws is None or not bool(getattr(ws, "diag_k8s_expert_rag_enabled", True)):
        return ""
    raw = (query_text or "").strip()
    if len(raw) < 16:
        return ""
    coll = _expert_collection(ctx)
    try:
        resp = await ctx.vector_store.similarity_search(
            raw[: int(getattr(ws, "diag_k8s_expert_rag_query_max_chars", 4000))],
            coll,
            llm=ctx.llm,
            embed_model=ws.embed_model,
            limit=int(getattr(ws, "diag_k8s_expert_rag_limit", 4)),
            score_threshold=float(getattr(ws, "diag_k8s_expert_rag_score_threshold", 0.40)),
        )
    except Exception as e:
        logger.debug("diag k8s_expert rag: %s", e)
        return ""

    max_c = int(getattr(ws, "diag_k8s_expert_rag_max_chars", 3200))
    parts: list[str] = []
    used = 0
    for pt in resp.points or []:
        pl = dict(pt.payload or {})
        meta = pl.get("metadata") if isinstance(pl.get("metadata"), dict) else {}
        url = str(meta.get("url") or "")
        ver = str(meta.get("version") or "")
        chunk = (pl.get("text") or pl.get("summary") or "").strip()
        if not chunk:
            continue
        sc = float(getattr(pt, "score", None) or 0.0)
        block = f"[CONTEXT: k8s_expert score={sc:.3f} version={ver} url={url}]\n{chunk[:1400]}"
        if used + len(block) > max_c:
            break
        parts.append(block)
        used += len(block) + 2
    return "\n\n".join(parts)


def _embedding_from_response(resp: dict[str, Any]) -> list[float]:
    if "embedding" in resp:
        emb = resp["embedding"]
        return list(emb) if not isinstance(emb, list) else emb
    embs = resp.get("embeddings")
    if isinstance(embs, list) and embs:
        return list(embs[0])
    raise ValueError("embed response missing embedding(s)")


async def fetch_infra_injection_for_fallback(ctx: Any, user_text: str) -> str:
    """
    Bơm máu cho SRE Fallback: semantic expert (``OMNI_PGVECTOR_COLLECTION_K8S_EXPERT``) + ``infra_topology``.
    Không còn snapshot Redis ``state:current_topology`` / ``infra:learned:meta``.
    """
    parts: list[str] = []
    raw = (user_text or "").strip()
    if len(raw) < 8:
        return ""
    try:
        emb_resp = await ctx.llm.embed(
            model=ctx.settings.embed_model,
            input=raw[:2000],
        )
        vector = _embedding_from_response(emb_resp)
        ec = _expert_collection(ctx)
        for coll, label, lim, thresh in (
            (ec, "k8s_expert", 2, 0.48),
            (COLLECTION_INFRA_TOPOLOGY, "infra_topology", 2, 0.52),
        ):
            try:
                resp = await ctx.vector_store.query_points(
                    collection_name=coll,
                    query=vector,
                    limit=lim,
                    score_threshold=thresh,
                    with_payload=True,
                )
            except Exception as e:
                logger.debug("fallback vector %s: %s", coll, e)
                continue
            for pt in resp.points or []:
                pl = dict(pt.payload or {})
                chunk = pl.get("text") or pl.get("summary") or ""
                if chunk:
                    parts.append(f"[{label} score={pt.score:.3f}]\n{str(chunk)[:1600]}")
    except Exception as e:
        logger.debug("fallback infra embed: %s", e)
    if not parts:
        return ""
    return "\n\n".join(parts)


async def enrich_working_text_with_infra(
    ctx: Any,
    user_text: str,
    learned: LearnedContext | None = None,
) -> str:
    """Tái dùng kết quả preflight (blocks + vector); không đọc Redis topology."""
    raw = (user_text or "").strip()
    if len(raw) < 8:
        return raw

    if learned is not None and (learned.had_vector_search or learned.infra_blocks):
        blocks = list(learned.infra_blocks)
        hint = f"ns={learned.namespace or '-'} pod={learned.pod_name or '-'} svc={learned.service_name or '-'}"
        blocks.append(f"[CONTEXT: hints]\n{hint}")
        merged = "\n\n".join(blocks) + f"\n\n[USER_MESSAGE]\n{raw}"
        return _apply_infra_enrich_cap(ctx, merged)

    blocks: list[str] = []
    try:
        emb_resp = await ctx.llm.embed(
            model=ctx.settings.embed_model,
            input=raw[:2000],
        )
        vector = _embedding_from_response(emb_resp)
        ec = _expert_collection(ctx)
        for coll, label, lim, thresh in (
            (ec, "k8s_expert", 2, 0.48),
            (COLLECTION_INFRA_TOPOLOGY, "infra_topology", 3, 0.52),
        ):
            try:
                resp = await ctx.vector_store.query_points(
                    collection_name=coll,
                    query=vector,
                    limit=lim,
                    score_threshold=thresh,
                    with_payload=True,
                )
            except Exception as e:
                logger.debug("infra postgres %s: %s", coll, e)
                continue
            for pt in resp.points or []:
                pl = dict(pt.payload or {})
                chunk = pl.get("text") or pl.get("summary") or ""
                if chunk:
                    blocks.append(f"[CONTEXT: {label} score={pt.score:.3f}]\n{chunk[:1800]}")
    except Exception as e:
        logger.debug("infra postgres: %s", e)
    if not blocks:
        return raw
    merged = "\n\n".join(blocks) + f"\n\n[USER_MESSAGE]\n{raw}"
    return _apply_infra_enrich_cap(ctx, merged)


def _apply_infra_enrich_cap(ctx: Any, merged: str) -> str:
    cap = int(getattr(getattr(ctx, "settings", None), "infra_enrich_max_total_chars", 6000) or 6000)
    if len(merged) <= cap:
        return merged
    logger.info(
        "event=infra_enrich_capped original_len=%s cap=%s",
        len(merged),
        cap,
    )
    return merged[:cap]
