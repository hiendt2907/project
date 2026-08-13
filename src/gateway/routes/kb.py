"""RAG Knowledge-Base routes — list/create/delete vendor knowledge directly in Redis.

Reads live from the Redis Stack vector store (FT.SEARCH per collection) so the UI
shows BOTH pre-existing knowledge (SOP, SRE, k8s_expert, action_experience, ...) and
newly created entries. Create embeds via Ollama (stdlib urllib helper — gateway never
imports workers) and upserts into the HNSW index used by the diagnosis brain.
"""

from __future__ import annotations

import json
import logging
import os
import re
import struct
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from redis.commands.search.query import Query

log = logging.getLogger(__name__)

router = APIRouter(prefix="/kb", tags=["kb"])

EMBED_DIM = int(os.environ.get("OMNI_EMBED_DIM", "768"))  # must match redis_vector_store.EMBED_DIM

# Collection surfaced in the KB tab. `vendor_knowledge` is the write target for new
# entries; the rest are existing knowledge partitions shown read-through ("cũ").
KB_WRITE_COLLECTION = "vendor_knowledge"
KB_COLLECTIONS: tuple[str, ...] = (
    "vendor_knowledge",
    "k8s_expert",
    "SRE_KNOWLEDGE",
    "itops_sop_ledger",
    "action_experience",
    "os_hard_fail_diagnostic",
)

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_DEFAULT_SCORE = 60


def _get_redis(request: Request) -> Any:
    r = getattr(request.app.state, "redis", None)
    if r is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return r


class KbCreate(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    knowledge: str = Field(..., min_length=10, max_length=8000)
    vendor: str = Field("", max_length=80)
    category: str = Field("", max_length=80)
    tier: str = Field("basic", max_length=24)  # basic | intermediate | advanced
    situation: str = Field("", max_length=2000)
    score: int = Field(_DEFAULT_SCORE, ge=0, le=100)
    collection: str = Field(KB_WRITE_COLLECTION, max_length=64)


def _doc_score(payload: dict[str, Any]) -> int:
    """KB quality score 0-100: explicit payload.score, else map confidence, else default."""
    raw = payload.get("score")
    if isinstance(raw, (int, float)):
        return max(0, min(100, int(raw)))
    conf = str(payload.get("confidence", "")).lower()
    return {"high": 85, "medium": 65, "low": 45}.get(conf, _DEFAULT_SCORE)


def _summarize(payload: dict[str, Any], text_content: str) -> str:
    for k in ("title", "summary", "knowledge", "advisory", "text"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:280]
    return (text_content or "").strip()[:280]


async def _list_collection(redis: Any, collection: str, limit: int) -> list[dict[str, Any]]:
    idx = f"idx:{collection}"
    try:
        q = (
            Query("*")
            .paging(0, max(1, limit))
            .return_fields("omni_payload", "payload", "text_content")
            .dialect(2)
        )
        res = await redis.ft(idx).search(q)
    except Exception as e:  # index missing / empty collection — not fatal
        log.debug("event=kb_list_skip collection=%s err=%s", collection, e)
        return []

    out: list[dict[str, Any]] = []
    for doc in res.docs:
        try:
            raw = getattr(doc, "omni_payload", None) or getattr(doc, "payload", None) or "{}"
            payload = json.loads(raw)
        except Exception:
            payload = {}
        text_content = getattr(doc, "text_content", "") or ""
        doc_id = str(doc.id).split(":")[-1]
        stale_for_raw = payload.get("stale_for", [])
        stale_for = stale_for_raw if isinstance(stale_for_raw, list) else []
        out.append(
            {
                "id": doc_id,
                "collection": collection,
                "title": _summarize(payload, text_content),
                "vendor": str(payload.get("vendor", "")),
                "category": str(payload.get("category", payload.get("type", ""))),
                "tier": str(payload.get("tier", "")),
                "score": _doc_score(payload),
                "source": str(payload.get("source", "")),
                "editable": collection == KB_WRITE_COLLECTION,
                "confirmed_count": int(payload.get("confirmed_count", 0)),
                "contradicted_count": int(payload.get("contradicted_count", 0)),
                "stale": bool(payload.get("stale", False)),
                "stale_for": stale_for,
            }
        )
    return out


@router.get("")
async def list_kb(request: Request, limit: int = 200) -> JSONResponse:
    """List KB entries across all surfaced collections (old + new), highest score first."""
    redis = _get_redis(request)
    per = max(10, min(500, limit))
    items: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for col in KB_COLLECTIONS:
        rows = await _list_collection(redis, col, per)
        counts[col] = len(rows)
        items.extend(rows)
    items.sort(key=lambda r: r["score"], reverse=True)
    return JSONResponse(
        content={"items": items, "total": len(items), "counts": counts, "write_collection": KB_WRITE_COLLECTION}
    )


@router.post("")
async def create_kb(body: KbCreate, request: Request) -> JSONResponse:
    """Embed knowledge via Ollama and upsert into the HNSW index used by the diagnosis brain."""
    redis = _get_redis(request)
    collection = body.collection if _ID_RE.match(body.collection or "") else KB_WRITE_COLLECTION

    text = f"{body.title}\n{body.situation}\n{body.knowledge}".strip()
    try:
        from pkg.rag.ollama_embed import embed_text

        vector = await embed_text(text)
    except Exception as e:
        log.warning("event=kb_embed_failed err=%s", e)
        raise HTTPException(status_code=502, detail=f"embedding failed (Ollama): {e}") from e

    entry_id = f"kb-{uuid.uuid4().hex[:12]}"
    payload = {
        "title": body.title,
        "summary": body.title,
        "knowledge": body.knowledge,
        "situation": body.situation,
        "vendor": body.vendor,
        "category": body.category,
        "tier": body.tier,
        "score": int(body.score),
        "source": "kb_ui",
        "type": "vendor_kb",
        "text": text,
        "created_at": int(time.time()),
    }
    key = f"doc:{collection}:{entry_id}"
    text_content = f"{body.title} {body.knowledge}"[:4000]
    try:
        await redis.hset(
            key,
            mapping={
                "embedding": struct.pack(f"{EMBED_DIM}f", *vector),
                "omni_payload": json.dumps(payload, ensure_ascii=False),
                "text_content": text_content,
                "source": "kb_ui",
                "doc_type": "vendor_kb",
            },
        )
    except Exception as e:
        log.error("event=kb_upsert_failed err=%s", e)
        raise HTTPException(status_code=503, detail=f"redis upsert failed: {e}") from e

    log.info("event=kb_created id=%s collection=%s score=%d", entry_id, collection, body.score)
    return JSONResponse(content={"ok": True, "id": entry_id, "collection": collection}, status_code=201)


@router.delete("/{collection}/{entry_id}")
async def delete_kb(collection: str, entry_id: str, request: Request) -> JSONResponse:
    """Delete a single KB document (only entries created via the UI are deletable)."""
    redis = _get_redis(request)
    if not _ID_RE.match(collection) or not _ID_RE.match(entry_id):
        raise HTTPException(status_code=400, detail="invalid id")
    key = f"doc:{collection}:{entry_id}"
    try:
        removed = await redis.delete(key)
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if not removed:
        raise HTTPException(status_code=404, detail="not found")
    log.info("event=kb_deleted id=%s collection=%s", entry_id, collection)
    return JSONResponse(content={"ok": True, "id": entry_id})
