"""Unified RagGate: k8s_expert semantic search — HIT → formatted text, no LLM."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from rag.pgvector_store import COLLECTION_K8S_EXPERT

logger = logging.getLogger(__name__)

_MIN_QUERY_LEN = 12
_MAX_QUERY_LEN_DEFAULT = 8000


@dataclass
class RagGateOutcome:
    hit: bool
    formatted: str = ""
    best_score: float | None = None
    collection: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    #: Primary matched chunk (English, from official k8s ingest) for executor contract.
    match_text_en: str = ""
    suggested_tool: str = "kubectl_describe_pod"


def normalize_rag_query(raw_text: str, hints: dict[str, str] | None) -> str:
    """GIGO: chuỗi embed cho RAG — gộp hints an toàn (chỉ str)."""
    parts: list[str] = []
    t = (raw_text or "").strip()
    if hints:
        ns = hints.get("namespace") if isinstance(hints.get("namespace"), str) else None
        pod = hints.get("pod_name") or hints.get("pod")
        pod = pod if isinstance(pod, str) else None
        svc = hints.get("service_name") if isinstance(hints.get("service_name"), str) else None
        if ns:
            parts.append(f"namespace={ns}")
        if pod:
            parts.append(f"pod={pod}")
        if svc:
            parts.append(f"service={svc}")
    if parts:
        t = " ".join(parts) + "\n" + t
    t = t.strip()
    return t


def _format_hits(
    points: list[Any],
    *,
    max_words: int,
    max_block_chars: int,
) -> str:
    from workers.ollama_prompts_en import truncate_plain_text_to_max_words

    lines: list[str] = []
    for pt in points:
        pl = dict(getattr(pt, "payload", None) or {})
        meta = pl.get("metadata") if isinstance(pl.get("metadata"), dict) else {}
        url = str(meta.get("url") or "")
        ver = str(meta.get("version") or "")
        sc = float(getattr(pt, "score", None) or 0.0)
        chunk = (pl.get("text") or pl.get("summary") or "").strip()
        if not chunk:
            continue
        chunk = chunk[:max_block_chars]
        block = f"[CONTEXT: k8s_expert score={sc:.3f} version={ver} url={url}]\n{chunk}"
        lines.append(block)
    raw = "\n\n".join(lines)
    return truncate_plain_text_to_max_words(raw, max_words=max_words)


def _primary_match_excerpt(pt: Any) -> tuple[str, str]:
    """English excerpt + suggested next CLI-style tool id (contract, not execution)."""
    pl = dict(getattr(pt, "payload", None) or {})
    meta = pl.get("metadata") if isinstance(pl.get("metadata"), dict) else {}
    chunk = (pl.get("text") or pl.get("summary") or "").strip()
    chunk = chunk.lstrip(". \t\n\r")
    dtype = str(meta.get("type") or "").lower()
    if "troubleshoot" in dtype or "task" in dtype:
        tool = "kubectl_describe_pod"
    elif "reference" in dtype:
        tool = "kubectl_get_events"
    else:
        tool = "kubectl_describe_pod"
    return chunk[:8000], tool


async def evaluate_rag_gate(
    ctx: Any,
    raw_text: str,
    *,
    hints: dict[str, str] | None = None,
    trace: str | None = None,
) -> RagGateOutcome:
    """
    similarity_search trên collection ``pgvector_collection_k8s_expert``.
    HIT khi best_score >= threshold và có chunk hợp lệ.
    """
    ws = getattr(ctx, "settings", None)
    if ws is None or not bool(getattr(ws, "rag_gate_enabled", True)):
        return RagGateOutcome(hit=False, detail={"reason": "disabled"})

    q = normalize_rag_query(raw_text, hints)
    if len(q) < _MIN_QUERY_LEN:
        return RagGateOutcome(hit=False, detail={"reason": "query_too_short", "len": len(q)})

    coll_raw = getattr(ws, "pgvector_collection_k8s_expert", None)
    collection = (
        coll_raw.strip()
        if isinstance(coll_raw, str) and coll_raw.strip()
        else COLLECTION_K8S_EXPERT
    )

    qmax = int(getattr(ws, "rag_gate_query_max_chars", _MAX_QUERY_LEN_DEFAULT))
    limit = int(getattr(ws, "rag_gate_limit", 4))
    thr = float(getattr(ws, "rag_gate_score_threshold", 0.42))
    max_words = int(getattr(ws, "omni_summary_max_words", 100))
    block_cap = int(getattr(ws, "rag_gate_chunk_max_chars", 1200))

    try:
        resp = await ctx.vector_store.similarity_search(
            q[:qmax],
            collection,
            ollama=ctx.ollama,
            embed_model=ws.embed_model,
            keep_alive=ws.ollama_keep_alive,
            limit=limit,
            score_threshold=thr,
            query_max_chars=qmax,
        )
    except Exception as e:
        logger.warning("rag_gate search failed trace=%s err=%s", trace, e)
        return RagGateOutcome(
            hit=False,
            detail={"reason": "search_error", "error": str(e)[:200]},
        )

    pts = list(resp.points or [])
    if not pts:
        return RagGateOutcome(
            hit=False,
            collection=collection,
            detail={"reason": "no_points", "threshold": thr},
        )

    best = float(getattr(pts[0], "score", None) or 0.0)
    if best < thr:
        return RagGateOutcome(
            hit=False,
            best_score=best,
            collection=collection,
            detail={"reason": "below_threshold", "threshold": thr},
        )

    formatted = _format_hits(pts, max_words=max_words, max_block_chars=block_cap)
    if not formatted.strip():
        return RagGateOutcome(
            hit=False,
            best_score=best,
            collection=collection,
            detail={"reason": "empty_chunks"},
        )

    mt_en, sug_tool = _primary_match_excerpt(pts[0])
    logger.info(
        "event=rag_gate_hit trace=%s collection=%s best_score=%.4f words_cap=%s",
        trace,
        collection,
        best,
        max_words,
    )
    return RagGateOutcome(
        hit=True,
        formatted=formatted,
        best_score=best,
        collection=collection,
        detail={"n_points": len(pts)},
        match_text_en=mt_en,
        suggested_tool=sug_tool,
    )
