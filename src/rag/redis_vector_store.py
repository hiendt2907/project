"""Redis Stack vector store (RedisSearch + HASH type) — drop-in replacement for pgvector_store.py.

Public interface is identical to PGVectorStore so all callers work unchanged.
Storage: HASH per document, FT index per collection (HNSW COSINE on FLOAT32 embeddings).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import struct
import uuid
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.commands.search.field import TagField, TextField, VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

from rag.rag_freshness import stamp_freshness

def _inc_rag_empty(collection: str, search_type: str) -> None:
    """No-op until the process wires in a real metric — see set_rag_empty_result_hook()."""


def set_rag_empty_result_hook(fn) -> None:
    """Dependency-injection point: workers/ wires its metrics counter in at startup
    (see build_context() in omni_worker.py) instead of rag/ importing workers/ directly
    (rag/ must not import workers/ — INV dependency direction)."""
    global _inc_rag_empty
    _inc_rag_empty = fn

# nomic-embed-text (Ollama) = 768 dims; nv-embedqa-e5-v5 (NVIDIA NIM) = 1024 dims.
# Switching OMNI_LLM_PROVIDER requires recreating the HNSW index (dim is fixed at
# FT.CREATE time) and re-embedding existing entries — see pkg/rag/ollama_embed.py.
#
# ⚠️ Đã trả giá thật 2026-08-17 (roadmap A6): `_ensure_index()` chỉ tạo index nếu CHƯA tồn tại
# (FT.INFO thành công → skip) — đổi OMNI_EMBED_DIM trên ConfigMap KHÔNG tự động re-create index
# cũ. Kết quả: ghi 8000 vector 1024-dim vào index vẫn còn dim=768 từ trước → RediSearch từ chối
# ÂM THẦM toàn bộ ("Could not add vector with blob size 4096 (expected size 3072)",
# `FT.INFO <idx> | hash_indexing_failures`), `num_docs` giữ nguyên 0, không exception nào raise
# lên tầng gọi ingest — nhìn qua tưởng ingest thành công (log "upserted 8000/8000") nhưng RAG tra
# cứu vẫn trả về rỗng. Nếu đổi OMNI_EMBED_DIM lần nữa: PHẢI `FT.DROPINDEX idx:<collection>` (không
# kèm DD, giữ lại doc) trước khi ingest, để `_ensure_index()` tạo lại đúng dim mới — RediSearch sẽ
# tự backfill index cho các HASH key đã tồn tại khớp prefix, không cần chạy lại toàn bộ ingest.
EMBED_DIM = int(os.environ.get("OMNI_EMBED_DIM", "768"))

COLLECTION_SOP = "itops_sop_ledger"
COLLECTION_SOP_V2 = "itops_sop_ledger_v2"
COLLECTION_ERRORS = "itops_error_ledger"
COLLECTION_INFRA_TOPOLOGY = "infra_topology"
COLLECTION_ACTION_EXPERIENCE = "action_experience"
COLLECTION_CLI_HIL_CONTEXT = "cli_hil_context"
COLLECTION_VENDOR_KNOWLEDGE = "vendor_knowledge"
# Legacy partition; RAG "expert" default = k8s_expert
COLLECTION_SRE_KNOWLEDGE = "SRE_KNOWLEDGE"
# Official / unified expert knowledge
COLLECTION_K8S_EXPERT = "k8s_expert"
COLLECTION_DIAGNOSTIC_HISTORY = "diagnostic_history"
COLLECTION_OS_HARD_FAIL_DIAGNOSTIC = "os_hard_fail_diagnostic"

_ALL_KNOWN_COLLECTIONS = (
    COLLECTION_SOP,
    COLLECTION_SOP_V2,
    COLLECTION_ERRORS,
    COLLECTION_INFRA_TOPOLOGY,
    COLLECTION_ACTION_EXPERIENCE,
    COLLECTION_CLI_HIL_CONTEXT,
    COLLECTION_VENDOR_KNOWLEDGE,
    COLLECTION_SRE_KNOWLEDGE,
    COLLECTION_K8S_EXPERT,
    COLLECTION_DIAGNOSTIC_HISTORY,
    COLLECTION_OS_HARD_FAIL_DIAGNOSTIC,
)

# RedisSearch special characters that must be escaped in query strings
_FT_SPECIAL = re.compile(r'([-@{}\(\)|!*~+":;\[\]^\\])')


def _ft_escape(text: str) -> str:
    """Escape RedisSearch FT special chars in a query string."""
    return _FT_SPECIAL.sub(r"\\\1", text or "")


DEFAULT_TENANT_ID = "default"
_TENANT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def validate_tenant_id(tenant_id: str) -> str:
    """Validate *tenant_id* against the allowed charset; raise on bad input."""
    if not _TENANT_ID_RE.match(tenant_id or ""):
        raise ValueError(f"invalid tenant_id: {tenant_id!r}")
    return tenant_id


def scoped_collection_name(collection_name: str, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    """Return the tenant-scoped collection/index name.

    The default tenant keeps the legacy unscoped name so existing lab data
    (HNSW indexes already populated) stays reachable without a migration.
    Any other tenant gets its own isolated index: ``f"{collection_name}:{tenant_id}"``.
    """
    tid = validate_tenant_id(tenant_id)
    if tid == DEFAULT_TENANT_ID:
        return collection_name
    return f"{collection_name}:{tid}"


def _make_index_fields() -> list:
    return [
        VectorField(
            "embedding",
            "HNSW",
            {
                "TYPE": "FLOAT32",
                "DIM": str(EMBED_DIM),
                "DISTANCE_METRIC": "COSINE",
                "INITIAL_CAP": "10000",
                "M": "16",
                "EF_CONSTRUCTION": "64",
            },
            as_name="embedding",
        ),
        TextField("text_content", as_name="text_content"),
        TagField("source", as_name="source"),
        TagField("doc_type", as_name="doc_type"),
    ]


# ---------------------------------------------------------------------------
# Data models (identical to pgvector_store.py public surface)
# ---------------------------------------------------------------------------


class PointStruct(BaseModel):
    id: str
    vector: list[float] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    score: float | None = None


class QueryResponse(BaseModel):
    points: list[PointStruct]


# ---------------------------------------------------------------------------
# Embedding helpers (copied verbatim from pgvector_store.py)
# ---------------------------------------------------------------------------


def _stable_vec_from_text(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Deterministic unit vector for error payloads (no LLM call)."""
    h = hashlib.sha256(text.encode("utf-8")).digest()
    out: list[float] = []
    i = 0
    while len(out) < dim:
        chunk = h[i % len(h)]
        out.append((chunk / 127.5) - 1.0)
        i += 1
        if i % len(h) == 0:
            h = hashlib.sha256(h).digest()
    norm = math.sqrt(sum(x * x for x in out)) or 1.0
    return [x / norm for x in out]


def _is_embed_bad_request(e: BaseException) -> bool:
    try:
        import httpx

        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 400:
            return True
    except Exception:
        pass
    msg = str(e).lower()
    return "400" in msg or "bad request" in msg or "status code 400" in msg


async def _embed_query_robust(
    llm: Any,
    query: str,
    *,
    embed_model: str,
    embed_model_fallback: str | None,
    keep_alive: str,
    query_max_chars: int,
) -> list[float]:
    """Truncate + tiered retry on 400; optional fallback model (same dim as index)."""
    from pkg.rag.embed_utils import truncate_for_embedding

    base = (query or "")[: int(query_max_chars)]
    tiers = (512, 256, 128, 64)
    models: list[str] = [embed_model]
    fb = (embed_model_fallback or "").strip()
    if fb and fb != embed_model:
        models.append(fb)

    last_err: BaseException | None = None
    for model in models:
        for tier in tiers:
            chunk = truncate_for_embedding(base, max_tokens=tier)
            try:
                emb_resp = await llm.embed(
                    model=model,
                    input=chunk,
                    keep_alive=keep_alive,
                )
                return _embedding_vector_from_response(emb_resp)
            except Exception as e:
                last_err = e
                if _is_embed_bad_request(e):
                    logger.warning(
                        "event=rag_llm_embed_failed model=%s tier_tokens=%s embed_400_retry err=%s",
                        model,
                        tier,
                        str(e)[:220],
                    )
                    continue
                logger.warning("event=rag_llm_embed_failed model=%s err=%s", model, e)
                raise RuntimeError(f"rag_llm_embed_failed:{e!s}") from e
    raise RuntimeError(f"rag_llm_embed_failed:{last_err!s}") from last_err


def _embedding_vector_from_response(resp: dict[str, Any]) -> list[float]:
    if "embedding" in resp:
        emb = resp["embedding"]
        return list(emb) if isinstance(emb, list) else list(emb or [])
    embs = resp.get("embeddings")
    if isinstance(embs, list) and embs:
        e0 = embs[0]
        return list(e0) if isinstance(e0, list) else list(e0 or [])
    raise ValueError("embed response missing embedding(s)")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _before_retry_log(retry_state: Any) -> None:
    logger.warning(
        "Redis connection issue detected. Retrying... (Attempt: %s)",
        retry_state.attempt_number,
        exc_info=retry_state.outcome.exception(),
    )


def _docs_to_points(
    docs: list[Any],
    score_threshold: float | None,
) -> list[PointStruct]:
    """Convert raw redis-py search docs to PointStruct list."""
    pts: list[PointStruct] = []
    for doc in docs:
        # __score from HNSW is a cosine DISTANCE (lower = more similar)
        dist = float(getattr(doc, "__score", 1.0))
        score = 1.0 - dist
        if score_threshold is not None and score < score_threshold:
            continue
        try:
            # Field stored as "omni_payload" to avoid clash with Document(payload=) param.
            # Fallback to legacy "payload" field for entries written before the rename.
            raw = getattr(doc, "omni_payload", None) or getattr(doc, "payload", None) or "{}"
            payload = json.loads(raw)
        except Exception:
            payload = {}
        doc_id = doc.id.split(":")[-1]
        pts.append(PointStruct(id=doc_id, payload=payload, score=score))
    return pts


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class RedisVectorStore:
    """Redis Stack vector store (HASH + FT.SEARCH HNSW).

    Drop-in replacement for PGVectorStore — identical public interface.
    """

    def __init__(self, r: redis.Redis) -> None:
        self._r = r
        self._initialized_indexes: set[str] = set()
        self._initialized: bool = False

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    async def _ensure_index(self, collection_name: str) -> None:
        """Create FT index for *collection_name* if it does not exist."""
        if collection_name in self._initialized_indexes:
            return
        idx = f"idx:{collection_name}"
        try:
            await self._r.ft(idx).info()
            self._initialized_indexes.add(collection_name)
            return
        except Exception:
            # Index does not exist — fall through to create
            pass
        try:
            await self._r.ft(idx).create_index(
                _make_index_fields(),
                definition=IndexDefinition(
                    prefix=[f"doc:{collection_name}:"],
                    index_type=IndexType.HASH,
                ),
            )
            logger.info("event=redis_ft_index_created collection=%s", collection_name)
        except Exception as exc:
            # Race condition: another process created it between our INFO and CREATE
            if "index already exists" in str(exc).lower():
                logger.debug("event=redis_ft_index_already_exists collection=%s", collection_name)
            else:
                logger.warning(
                    "event=redis_ft_index_create_failed collection=%s err=%s",
                    collection_name,
                    exc,
                )
        self._initialized_indexes.add(collection_name)

    async def ensure_ready(self) -> None:
        """Create FT indexes for ALL known collections.

        Idempotent — safe to call multiple times.
        """
        if self._initialized:
            return
        for col in _ALL_KNOWN_COLLECTIONS:
            await self._ensure_index(col)
        self._initialized = True

    async def ensure_partition_for_collection(
        self, collection_name: str, tenant_id: str = DEFAULT_TENANT_ID
    ) -> None:
        """Create FT index for a dynamic collection name.

        Validates the name with the same regex used in pgvector_store.py.
        """
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]{0,62}$", str(collection_name)):
            raise ValueError(f"invalid collection_name: {collection_name!r}")
        await self._ensure_index(scoped_collection_name(collection_name, tenant_id))

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type(RedisConnectionError),
        reraise=True,
        before_sleep=_before_retry_log,
    )
    async def upsert(
        self,
        collection_name: str,
        points: list[Any],
        *,
        ttl_sec: int | None = None,
        cluster_version: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        """Batch-upsert *points* into *collection_name* using a Redis pipeline.

        ``ttl_sec`` caps unbounded rolling-memory collections (e.g.
        ``diagnostic_history``) so the AOF/RDB does not grow without limit.

        ``cluster_version`` stamps each chunk for RAG freshness / DEPRECATED_RISK
        drift detection (plan step 4). ``ingested_at`` is always stamped (preserved
        on re-upsert) so recall can age chunks. Both land in the payload AND as
        dedicated HASH fields for visibility.

        ``tenant_id`` isolates the underlying HNSW index per tenant — see
        ``scoped_collection_name()``.
        """
        collection_name = scoped_collection_name(collection_name, tenant_id)
        await self._ensure_index(collection_name)
        now_iso = datetime.now(UTC).isoformat()
        pipe = self._r.pipeline(transaction=False)
        for p in points:
            pid = getattr(p, "id", None) or p["id"]
            pvec = getattr(p, "vector", None) or (p.get("vector") if isinstance(p, dict) else None)
            ppayload: dict[str, Any] = (
                getattr(p, "payload", None)
                if not isinstance(p, dict)
                else p.get("payload", {})
            ) or {}

            if pvec is None:
                logger.warning("event=redis_upsert_skip_no_vector id=%s", pid)
                continue

            # Stamp freshness metadata immutably (returns a copy).
            ppayload = stamp_freshness(
                ppayload, cluster_version=cluster_version, now_iso=now_iso
            )

            key = f"doc:{collection_name}:{pid}"
            text_content = (
                str(ppayload.get("text", "")) + " " + str(ppayload.get("summary", ""))
            )[:4000]
            vec_bytes = struct.pack(f"{EMBED_DIM}f", *pvec)
            pipe.hset(
                key,
                mapping={
                    "embedding": vec_bytes,
                    # "omni_payload" avoids clash with Document(payload=) constructor param
                    "omni_payload": json.dumps(ppayload),
                    "text_content": text_content,
                    "source": str(ppayload.get("source", "")),
                    "doc_type": str(ppayload.get("type", "")),
                    "ingested_at": str(ppayload.get("ingested_at", now_iso)),
                    "cluster_version": str(ppayload.get("cluster_version", "")),
                },
            )
            if ttl_sec and ttl_sec > 0:
                pipe.expire(key, ttl_sec)
        try:
            await pipe.execute()
        except Exception as exc:
            logger.warning("event=redis_upsert_pipeline_failed collection=%s err=%s", collection_name, exc)
            raise

    # ------------------------------------------------------------------
    # Read — vector search
    # ------------------------------------------------------------------

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type(RedisConnectionError),
        reraise=True,
        before_sleep=_before_retry_log,
    )
    async def query_points(
        self,
        collection_name: str,
        query: list[float],
        limit: int = 1,
        score_threshold: float | None = None,
        with_payload: bool = True,
        payload_filters: dict[str, str] | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> QueryResponse:
        """Pure KNN search using HNSW index."""
        collection_name = scoped_collection_name(collection_name, tenant_id)
        await self._ensure_index(collection_name)
        lim = max(1, int(limit))
        vec_bytes = struct.pack(f"{EMBED_DIM}f", *query)
        q = (
            Query(f"*=>[KNN {lim} @embedding $vec AS __score]")
            .sort_by("__score", asc=True)
            .return_fields("omni_payload", "payload", "__score")
            .paging(0, lim)
            .dialect(2)
        )
        try:
            results = await self._r.ft(f"idx:{collection_name}").search(
                q, query_params={"vec": vec_bytes}
            )
        except Exception as exc:
            logger.warning(
                "event=redis_query_points_failed collection=%s err=%s", collection_name, exc
            )
            raise

        pts: list[PointStruct] = []
        for doc in results.docs:
            dist = float(getattr(doc, "__score", 1.0))
            score = 1.0 - dist  # convert COSINE distance → similarity
            if score_threshold is not None and score < score_threshold:
                continue
            payload: dict[str, Any] = {}
            if with_payload:
                try:
                    raw = getattr(doc, "omni_payload", None) or getattr(doc, "payload", None) or "{}"
                    payload = json.loads(raw)
                except Exception:
                    payload = {}
            # Apply optional payload_filters (post-filter, same semantics as pgvector)
            if payload_filters:
                match = all(
                    payload.get(k) == v
                    for k, v in payload_filters.items()
                    if v is not None and isinstance(v, str)
                    and re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", str(k))
                )
                if not match:
                    continue
            doc_id = doc.id.split(":")[-1]
            pts.append(PointStruct(id=doc_id, payload=payload, score=score))
        return QueryResponse(points=pts)

    # ------------------------------------------------------------------
    # Read — full-text search
    # ------------------------------------------------------------------

    async def fulltext_search_points(
        self,
        collection_name: str,
        query_text: str,
        limit: int = 12,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> QueryResponse:
        """FT search on text_content field (BM25 via RedisSearch)."""
        collection_name = scoped_collection_name(collection_name, tenant_id)
        qt = (query_text or "").strip()
        if len(qt) < 2:
            return QueryResponse(points=[])
        safe = _ft_escape(qt[:2000])
        if len(safe) < 2:
            return QueryResponse(points=[])
        lim = max(1, min(48, int(limit)))
        q = (
            Query(f"@text_content:({safe})")
            .return_fields("omni_payload", "payload")
            .with_scores()
            .paging(0, lim)
            .dialect(2)
        )
        try:
            results = await self._r.ft(f"idx:{collection_name}").search(q)
        except Exception as exc:
            logger.warning(
                "event=redis_fulltext_search_failed collection=%s err=%s", collection_name, exc
            )
            _inc_rag_empty(collection_name, "fulltext")
            return QueryResponse(points=[])

        pts: list[PointStruct] = []
        for doc in results.docs:
            try:
                raw = getattr(doc, "omni_payload", None) or getattr(doc, "payload", None) or "{}"
                payload = json.loads(raw)
            except Exception:
                payload = {}
            pts.append(
                PointStruct(
                    id=doc.id.split(":")[-1],
                    payload=payload,
                    score=float(getattr(doc, "score", 0.0)),
                )
            )
        return QueryResponse(points=pts)

    # ------------------------------------------------------------------
    # High-level search API (matches PGVectorStore exactly)
    # ------------------------------------------------------------------

    async def similarity_search(
        self,
        query: str,
        collection_id: str,
        *,
        llm: Any,
        embed_model: str,
        embed_model_fallback: str | None = None,
        keep_alive: str = "5m",
        limit: int = 8,
        score_threshold: float | None = None,
        query_max_chars: int = 8000,
        payload_filters: dict[str, str] | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> QueryResponse:
        """Embed *query* then cosine-search on *collection_id*."""
        if llm is None:
            raise TypeError("similarity_search requires llm client")
        vector = await _embed_query_robust(
            llm,
            query,
            embed_model=embed_model,
            embed_model_fallback=embed_model_fallback,
            keep_alive=keep_alive,
            query_max_chars=int(query_max_chars),
        )
        try:
            return await self.query_points(
                collection_name=collection_id,
                query=vector,
                limit=int(limit),
                score_threshold=score_threshold,
                with_payload=True,
                payload_filters=payload_filters,
                tenant_id=tenant_id,
            )
        except Exception as e:
            logger.warning("event=rag_redis_query_failed err=%s", e)
            raise RuntimeError(f"rag_redis_query_failed:{e!s}") from e

    async def similarity_search_by_vector(
        self,
        vector: list[float],
        collection_id: str,
        *,
        limit: int = 8,
        score_threshold: float | None = None,
        payload_filters: dict[str, str] | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> QueryResponse:
        """Cosine-search with a pre-computed embedding (skips re-embedding the query)."""
        try:
            return await self.query_points(
                collection_name=collection_id,
                query=vector,
                limit=int(limit),
                score_threshold=score_threshold,
                with_payload=True,
                payload_filters=payload_filters,
                tenant_id=tenant_id,
            )
        except Exception as e:
            logger.warning("event=rag_redis_query_failed err=%s", e)
            raise RuntimeError(f"rag_redis_query_failed:{e!s}") from e

    def _rrf_merge_points(
        self,
        dense: list[PointStruct],
        sparse: list[PointStruct],
        *,
        k: int = 60,
        dense_weight: float = 0.65,
    ) -> list[PointStruct]:
        """Reciprocal rank fusion for hybrid retrieval (app-layer fallback)."""
        w_d = max(0.0, min(1.0, float(dense_weight)))
        w_s = 1.0 - w_d
        scores: dict[str, float] = {}
        payloads: dict[str, dict[str, Any]] = {}
        for rank, p in enumerate(dense):
            pid = str(p.id)
            scores[pid] = scores.get(pid, 0.0) + w_d / (k + rank + 1)
            payloads.setdefault(pid, dict(p.payload or {}))
        for rank, p in enumerate(sparse):
            pid = str(p.id)
            scores[pid] = scores.get(pid, 0.0) + w_s / (k + rank + 1)
            payloads.setdefault(pid, dict(p.payload or {}))
        ordered = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        return [
            PointStruct(id=pid, payload=payloads.get(pid, {}), score=float(scores[pid]))
            for pid in ordered
        ]

    async def similarity_search_hybrid(
        self,
        query: str,
        collection_id: str,
        *,
        llm: Any,
        embed_model: str,
        embed_model_fallback: str | None = None,
        keep_alive: str = "5m",
        limit: int = 8,
        score_threshold: float | None = None,
        query_max_chars: int = 8000,
        payload_filters: dict[str, str] | None = None,
        hybrid_vector_weight: float = 0.65,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> QueryResponse:
        """Native Redis hybrid search (pre-filter text + KNN) with RRF app-layer fallback."""
        if llm is None:
            raise TypeError("similarity_search_hybrid requires llm client")
        vector = await _embed_query_robust(
            llm,
            query,
            embed_model=embed_model,
            embed_model_fallback=embed_model_fallback,
            keep_alive=keep_alive,
            query_max_chars=int(query_max_chars),
        )
        scoped_collection_id = scoped_collection_name(collection_id, tenant_id)
        await self._ensure_index(scoped_collection_id)
        vec_bytes = struct.pack(f"{EMBED_DIM}f", *vector)
        safe_text = _ft_escape((query or "")[:500])
        lim = max(1, int(limit))

        # Attempt native hybrid: text pre-filter + KNN
        if len(safe_text) >= 2:
            try:
                q = (
                    Query(
                        f"(@text_content:({safe_text}))=>[KNN {lim} @embedding $vec AS __score]"
                    )
                    .sort_by("__score", asc=True)
                    .return_fields("omni_payload", "__score")
                    .paging(0, lim)
                    .dialect(2)
                )
                results = await self._r.ft(f"idx:{scoped_collection_id}").search(
                    q, query_params={"vec": vec_bytes}
                )
                pts = _docs_to_points(results.docs, score_threshold)
                if pts:
                    return QueryResponse(points=pts)
            except Exception as exc:
                logger.warning("event=redis_hybrid_failed collection=%s err=%s", collection_id, exc)

        # Fallback: app-layer RRF (dense + sparse)
        dense_lim = max(lim * 3, 16)
        try:
            dense_resp = await self.query_points(
                collection_name=collection_id,
                query=vector,
                limit=dense_lim,
                score_threshold=None,
                with_payload=True,
                payload_filters=payload_filters,
                tenant_id=tenant_id,
            )
        except Exception as exc:
            logger.warning("event=rag_redis_query_failed err=%s", exc)
            raise RuntimeError(f"rag_redis_query_failed:{exc!s}") from exc

        sparse_resp = await self.fulltext_search_points(
            collection_id,
            (query or "")[: int(query_max_chars)],
            limit=max(12, lim * 3),
            tenant_id=tenant_id,
        )
        merged = self._rrf_merge_points(
            list(dense_resp.points or []),
            list(sparse_resp.points or []),
            dense_weight=float(hybrid_vector_weight),
        )
        dense_by_id = {str(p.id): float(p.score or 0.0) for p in (dense_resp.points or [])}
        out_pts: list[PointStruct] = []
        for p in merged:
            did = str(p.id)
            best_sim = dense_by_id.get(did)
            if best_sim is None:
                continue
            if score_threshold is not None and best_sim < float(score_threshold):
                continue
            out_pts.append(PointStruct(id=did, payload=dict(p.payload or {}), score=best_sim))
            if len(out_pts) >= lim:
                break
        if not out_pts and dense_resp.points:
            for p in dense_resp.points[:lim]:
                sc = float(getattr(p, "score", None) or 0.0)
                if score_threshold is not None and sc < float(score_threshold):
                    continue
                out_pts.append(p)
                if len(out_pts) >= lim:
                    break
        if not out_pts:
            _inc_rag_empty(collection_id, "hybrid")
        return QueryResponse(points=out_pts)

    # ------------------------------------------------------------------
    # Governance counters
    # ------------------------------------------------------------------

    async def action_experience_unique_counts(self) -> dict[str, int]:
        """Scan action_experience keys and count unique pattern_keys by outcome."""
        pattern = f"doc:{COLLECTION_ACTION_EXPERIENCE}:*"
        unique_success: set[str] = set()
        unique_fail: set[str] = set()
        cursor = 0
        while True:
            try:
                cursor, keys = await self._r.scan(cursor, match=pattern, count=200)
            except Exception as exc:
                logger.warning(
                    "event=redis_action_experience_scan_failed err=%s", exc
                )
                break
            for key in keys:
                try:
                    raw = await self._r.hget(key, "omni_payload")
                    if not raw:
                        continue
                    payload = json.loads(raw)
                except Exception:
                    continue
                pk = payload.get("pattern_key") or str(key).split(":")[-1]
                outcome = str(payload.get("exec_outcome", ""))
                if outcome == "success":
                    unique_success.add(pk)
                else:
                    unique_fail.add(pk)
            if cursor == 0:
                break
        return {
            "unique_success_patterns": len(unique_success),
            "unique_fail_patterns": len(unique_fail),
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """No-op — shared connection is owned by the caller."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class RedisRAGSettings(BaseSettings):
    """Env: ``OMNI_REDIS_URL``."""

    model_config = SettingsConfigDict(env_prefix="OMNI_", extra="ignore")

    redis_url: str = Field(default="redis://redis:6379/0")


# ---------------------------------------------------------------------------
# Backward-compatibility stubs
# ---------------------------------------------------------------------------

# Deprecated alias so code that imports PostgresRAGSettings still works
PostgresRAGSettings = RedisRAGSettings


async def init_pg_pool(*args: Any, **kwargs: Any) -> None:  # type: ignore[return]
    raise DeprecationWarning(
        "Postgres removed; use RedisVectorStore with existing Redis connection"
    )


# Alias for callers that have not yet been updated to the new class name
PGVectorStore = RedisVectorStore


# ---------------------------------------------------------------------------
# Error ledger helper
# ---------------------------------------------------------------------------


async def log_error_to_ledger(
    client: RedisVectorStore | redis.Redis,
    *,
    title: str,
    detail: str,
    phase: str,
    component: str = "",
    collection_name: str = COLLECTION_ERRORS,
    extra: dict[str, Any] | None = None,
) -> str:
    """Upsert a structured error entry into the error ledger collection."""
    payload_text = f"{title}\n{detail}::{phase}::{component}"
    vector = _stable_vec_from_text(payload_text)
    point_id_str = str(
        uuid.uuid5(uuid.NAMESPACE_URL, payload_text + str(datetime.now(UTC).date()))
    )
    payload: dict[str, Any] = {
        "title": title,
        "detail": detail,
        "phase": phase,
        "component": component,
        "ts": datetime.now(UTC).isoformat(),
    }
    if extra:
        payload["extra"] = extra

    store = client if isinstance(client, RedisVectorStore) else RedisVectorStore(client)
    await store.upsert(
        collection_name,
        [PointStruct(id=point_id_str, vector=vector, payload=payload)],
    )
    return point_id_str
