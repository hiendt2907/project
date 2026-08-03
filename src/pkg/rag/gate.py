"""Unified RagGate: k8s_expert semantic search — HIT → formatted text, no LLM."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

COLLECTION_K8S_EXPERT = "k8s_expert"  # mirrors rag.redis_vector_store — avoids src/rag/ dep in gateway image

from pkg.rag.embed_utils import truncate_for_embedding


def effective_reply_max_words(ws: Any) -> int:
    """Local copy — pkg layer must not import from workers.*."""
    sm = int(getattr(ws, "omni_summary_max_words", 100) or 100)
    c = getattr(ws, "omni_concise_reply_max_words", None)
    if c is None:
        return sm
    return min(max(10, int(c)), sm)

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
    #: Stable chunk ids (rag_documents.id) for Truth Law citations.
    chunk_ids: list[str] = field(default_factory=list)


_HTTP_ERROR_NOISE = re.compile(
    r"(?mis)^(Status:|Reason:|Message:|Metadata:|Details:|Name:|Group:|Version:|Kind:|Cause:|Field:|RetryAfterSeconds:).*$"
)
_LONG_BLOB_LINE = re.compile(r"^.{400,}$")


def clean_and_truncate_context(
    raw_text: str,
    hints: dict[str, str] | None,
    *,
    max_tokens: int = 512,
) -> str:
    """
    Strip API error noise / huge blobs before embedding. Prefer alert name, short reason, top event lines.
    """
    t = (raw_text or "").strip()
    lines = t.splitlines()
    kept: list[str] = []
    eventish = 0
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if _HTTP_ERROR_NOISE.match(s):
            continue
        if _LONG_BLOB_LINE.match(s):
            kept.append(s[:320] + "…")
            continue
        low = s.lower()
        if "traceback" in low or "file \"/" in low:
            continue
        if " normal " in low and "warning" in low and "error" in low:
            pass
        if low.startswith("warning ") or "warning:" in low[:12]:
            kept.append(s[:400])
            continue
        if eventish < 3 and (" normal " in low or " warning " in low or " error " in low):
            kept.append(s[:500])
            eventish += 1
            continue
        kept.append(s[:600])
    t2 = "\n".join(kept[:80]).strip()
    if hints:
        alertname = hints.get("alertname") or hints.get("alert_name")
        if isinstance(alertname, str) and alertname.strip():
            t2 = f"alert_name={alertname.strip()}\n" + t2
    return truncate_for_embedding(t2, max_tokens=max_tokens)


def normalize_rag_query(raw_text: str, hints: dict[str, str] | None) -> str:
    """GIGO: chuỗi embed cho RAG — gộp hints an toàn (chỉ str)."""
    parts: list[str] = []
    t = (raw_text or "").strip()
    if hints:
        ns = hints.get("namespace") if isinstance(hints.get("namespace"), str) else None
        pod = hints.get("pod_name") or hints.get("pod")
        pod = pod if isinstance(pod, str) else None
        svc = hints.get("service_name") if isinstance(hints.get("service_name"), str) else None
        an = hints.get("alertname") or hints.get("alert_name")
        an = an if isinstance(an, str) and an.strip() else None
        sg = hints.get("symptom_group") if isinstance(hints.get("symptom_group"), str) else None
        dp = hints.get("diagnostic_pattern") if isinstance(hints.get("diagnostic_pattern"), str) else None
        if ns:
            parts.append(f"namespace={ns}")
        if pod:
            parts.append(f"pod={pod}")
        if svc:
            parts.append(f"service={svc}")
        if an:
            parts.append(f"alertname={an}")
        if sg and sg.strip():
            parts.append(f"symptom_group={sg.strip()}")
        if dp and dp.strip():
            parts.append(f"diagnostic_pattern={dp.strip()}")
    if parts:
        t = " ".join(parts) + "\n" + t
    t = t.strip()
    return t


def _incident_like_query(raw_text: str) -> bool:
    """Heuristic: alert / incident context vs definitional lookup."""
    t = (raw_text or "").lower()
    if "batch_diagnostic_evidence" in t or "[alert_context]" in t:
        return True
    keys = (
        "crash",
        "oom",
        "timeout",
        "error",
        "fail",
        "backoff",
        "unavailable",
        "highcpu",
        "pending",
        "evicted",
    )
    return any(k in t for k in keys)


def _metadata_type(pl: dict[str, Any]) -> str:
    meta = pl.get("metadata") if isinstance(pl.get("metadata"), dict) else {}
    return str(meta.get("type") or "").lower()


def _post_filter_points_for_incident(
    points: list[Any],
    raw_text: str,
    *,
    enabled: bool,
) -> list[Any]:
    """Drop reference/glossary chunks when query looks like an incident (Phase A)."""
    if not enabled or not points:
        return points
    if not _incident_like_query(raw_text):
        return points
    out: list[Any] = []
    for pt in points:
        pl = dict(getattr(pt, "payload", None) or {})
        dt = _metadata_type(pl)
        if dt and "reference" in dt and "troubleshoot" not in dt and "task" not in dt:
            continue
        out.append(pt)
    return out if out else points


def _format_hits(
    points: list[Any],
    *,
    max_words: int,
    max_block_chars: int,
) -> tuple[str, list[str]]:
    from workers.llm_prompts_en import truncate_plain_text_to_max_words

    lines: list[str] = []
    chunk_ids: list[str] = []
    for pt in points:
        pl = dict(getattr(pt, "payload", None) or {})
        meta = pl.get("metadata") if isinstance(pl.get("metadata"), dict) else {}
        url = str(meta.get("url") or "")
        ver = str(meta.get("version") or "")
        sc = float(getattr(pt, "score", None) or 0.0)
        chunk = (pl.get("text") or pl.get("summary") or "").strip()
        if not chunk:
            continue
        cid = str(getattr(pt, "id", "") or "").strip() or "unknown"
        chunk_ids.append(cid)
        chunk = chunk[:max_block_chars]
        block = (
            f"[Source: RAG_CHUNK_{cid}]\n"
            f"[CONTEXT: k8s_expert score={sc:.3f} version={ver} url={url}]\n{chunk}"
        )
        lines.append(block)
    raw = "\n\n".join(lines)
    return truncate_plain_text_to_max_words(raw, max_words=max_words), chunk_ids


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


def _resolve_search_collection(ws: Any, raw_text: str) -> str:
    """Phase A2: route troubleshoot vs default expert collection when configured."""
    coll_raw = getattr(ws, "pgvector_collection_k8s_expert", None)
    default = (
        coll_raw.strip()
        if isinstance(coll_raw, str) and coll_raw.strip()
        else COLLECTION_K8S_EXPERT
    )
    ts = getattr(ws, "pgvector_collection_k8s_troubleshoot", None)
    troubleshoot = ts.strip() if isinstance(ts, str) and ts.strip() else ""
    if not troubleshoot or troubleshoot == default:
        return default
    if _incident_like_query(raw_text):
        return troubleshoot
    return default


async def evaluate_rag_gate(
    ctx: Any,
    raw_text: str,
    *,
    hints: dict[str, str] | None = None,
    trace: str | None = None,
) -> RagGateOutcome:
    """Evaluate the gate and record which way it went.

    The outcome is the "did this turn cost a real LLM call?" signal: a ``hit``
    (or ``cache_hit``) is answered from knowledge, anything else falls through
    to the model. Recording is best-effort and never alters the outcome.
    """
    outcome = await _evaluate_rag_gate_impl(ctx, raw_text, hints=hints, trace=trace)
    try:
        from pkg.observability.llm_observability import record_rag_gate

        reason = str((outcome.detail or {}).get("reason") or "")
        record_rag_gate(
            "hit" if outcome.hit and reason != "cache_hit" else (reason or "hit"),
            collection=outcome.collection or "",
            trace_id=trace,
        )
    except ImportError:
        pass
    except Exception as _exc:  # noqa: BLE001 — telemetry must not break the gate
        logger.warning("rag_gate: outcome record failed trace=%s err=%s", trace, _exc)
    return outcome


async def _evaluate_rag_gate_impl(
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
    import hashlib
    import json

    ws = getattr(ctx, "settings", None)
    if ws is None or not bool(getattr(ws, "rag_gate_enabled", True)):
        return RagGateOutcome(hit=False, detail={"reason": "disabled"})

    q = normalize_rag_query(raw_text, hints)
    if len(q) < _MIN_QUERY_LEN:
        return RagGateOutcome(hit=False, detail={"reason": "query_too_short", "len": len(q)})

    collection = _resolve_search_collection(ws, q)

    qmax = int(getattr(ws, "rag_gate_query_max_chars", _MAX_QUERY_LEN_DEFAULT))
    embed_cap = int(getattr(ws, "rag_embed_max_tokens", 512))
    q = clean_and_truncate_context(q[:qmax], hints, max_tokens=embed_cap)
    limit = int(getattr(ws, "rag_gate_limit", 4))
    thr = float(getattr(ws, "rag_gate_score_threshold", 0.42))
    max_words = effective_reply_max_words(ws)
    block_cap = int(getattr(ws, "rag_gate_chunk_max_chars", 1200))
    hybrid = bool(getattr(ws, "rag_hybrid_search_enabled", False))
    hot_ttl = int(getattr(ws, "rag_hot_cache_ttl_sec", 3600))
    hot_enabled = bool(getattr(ws, "rag_hot_cache_enabled", False))
    uncertain_thr = float(getattr(ws, "rag_tier_knowledge_uncertain_threshold", 0.7))
    tier_uncertain_on = bool(getattr(ws, "rag_tier_uncertain_gate_enabled", False))

    cache_key = f"rag:hot:{hashlib.sha256((collection + ':' + q).encode()).hexdigest()[:48]}"
    r = getattr(ctx, "redis", None)
    if hot_enabled and r is not None:
        try:
            cached = await r.get(cache_key)
            if cached:
                try:
                    raw = cached.decode() if isinstance(cached, bytes) else cached
                    obj = json.loads(raw)
                    if isinstance(obj, dict) and obj.get("formatted"):
                        return RagGateOutcome(
                            hit=bool(obj.get("hit")),
                            formatted=str(obj.get("formatted") or ""),
                            best_score=obj.get("best_score"),
                            collection=str(obj.get("collection") or collection),
                            detail={"reason": "cache_hit"},
                            match_text_en=str(obj.get("match_text_en") or ""),
                            suggested_tool=str(obj.get("suggested_tool") or "kubectl_describe_pod"),
                            chunk_ids=list(obj.get("chunk_ids") or []),
                        )
                except Exception:
                    pass
        except Exception as e:
            logger.debug("rag hot cache miss trace=%s err=%s", trace, e)

    try:
        emb_fb = getattr(ws, "embed_model_fallback", None)
        emb_fb_s = emb_fb.strip() if isinstance(emb_fb, str) else ""
        if hybrid and hasattr(ctx.vector_store, "similarity_search_hybrid"):
            resp = await ctx.vector_store.similarity_search_hybrid(
                q,
                collection,
                llm=ctx.llm,
                embed_model=ws.embed_model,
                embed_model_fallback=emb_fb_s or None,
                limit=limit,
                score_threshold=thr,
                query_max_chars=qmax,
                hybrid_vector_weight=float(getattr(ws, "rag_hybrid_vector_weight", 0.65)),
            )
        else:
            resp = await ctx.vector_store.similarity_search(
                q,
                collection,
                llm=ctx.llm,
                embed_model=ws.embed_model,
                embed_model_fallback=emb_fb_s or None,
                limit=limit,
                score_threshold=thr,
                query_max_chars=qmax,
            )
    except Exception as e:
        msg = str(e)
        if "rag_llm_embed_failed" in msg:
            phase = "llm_embed"
        elif "rag_pgvector_query_failed" in msg:
            phase = "pgvector_query"
        else:
            phase = "unknown"
        logger.warning(
            "rag_gate search failed trace=%s phase=%s err=%s",
            trace,
            phase,
            e,
        )
        return RagGateOutcome(
            hit=False,
            detail={"reason": "search_error", "phase": phase, "error": msg[:200]},
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

    if tier_uncertain_on and best < uncertain_thr:
        return RagGateOutcome(
            hit=False,
            best_score=best,
            collection=collection,
            detail={"reason": "knowledge_uncertain", "threshold": uncertain_thr},
        )

    pts = _post_filter_points_for_incident(
        pts,
        q,
        enabled=bool(getattr(ws, "rag_post_filter_metadata_enabled", True)),
    )
    if not pts:
        return RagGateOutcome(
            hit=False,
            best_score=best,
            collection=collection,
            detail={"reason": "post_filter_empty"},
        )

    formatted, chunk_ids = _format_hits(pts, max_words=max_words, max_block_chars=block_cap)
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
    out = RagGateOutcome(
        hit=True,
        formatted=formatted,
        best_score=best,
        collection=collection,
        detail={"n_points": len(pts), "chunk_ids": chunk_ids},
        match_text_en=mt_en,
        suggested_tool=sug_tool,
        chunk_ids=chunk_ids,
    )
    if hot_enabled and r is not None:
        try:
            await r.setex(
                cache_key,
                max(60, hot_ttl),
                json.dumps(
                    {
                        "hit": True,
                        "formatted": formatted,
                        "best_score": best,
                        "collection": collection,
                        "match_text_en": mt_en,
                        "suggested_tool": sug_tool,
                        "chunk_ids": chunk_ids,
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception as e:
            logger.debug("rag hot cache set fail trace=%s err=%s", trace, e)
    return out
