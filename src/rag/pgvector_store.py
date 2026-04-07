"""PostgreSQL HA with pgvector collections + error ledger (vectors + payload)."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
import asyncpg
from pgvector.asyncpg import register_vector
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

logger = logging.getLogger(__name__)

# nomic-embed-text (Ollama) = 768 dims
EMBED_DIM = 768

COLLECTION_SOP = "itops_sop_ledger"
COLLECTION_SOP_V2 = "itops_sop_ledger_v2"
COLLECTION_ERRORS = "itops_error_ledger"
COLLECTION_INFRA_TOPOLOGY = "infra_topology"
COLLECTION_ACTION_EXPERIENCE = "action_experience"
COLLECTION_CLI_HIL_CONTEXT = "cli_hil_context"
COLLECTION_VENDOR_KNOWLEDGE = "vendor_knowledge"
# Legacy partition (dữ liệu cũ); RAG “expert” mặc định = ``k8s_expert`` (OMNI_PGVECTOR_COLLECTION_K8S_EXPERT).
COLLECTION_SRE_KNOWLEDGE = "SRE_KNOWLEDGE"
# Official / unified expert knowledge (WorkerSettings.pgvector_collection_k8s_expert).
COLLECTION_K8S_EXPERT = "k8s_expert"


class PostgresRAGSettings(BaseSettings):
    """Env: `POSTGRES_RAG_DSN`."""

    model_config = SettingsConfigDict(env_prefix="POSTGRES_", extra="ignore")

    rag_dsn: str = Field(default="postgresql://appuser:GD3fjTJJxfzi0bau6TSaoWV9Q8TeuEYxahQrFDh6DCnMRjgFdEQ1q7Hf3FKFbxD8@pgpool-gateway:5432/ragdb")


async def init_pg_pool(settings: PostgresRAGSettings | None = None) -> asyncpg.Pool:
    s = settings or PostgresRAGSettings()
    
    async def init_connection(conn: asyncpg.Connection) -> None:
        await register_vector(conn)

    pool = await asyncpg.create_pool(
        dsn=s.rag_dsn,
        min_size=2,
        max_size=20,
        init=init_connection,
    )
    if not pool:
        raise RuntimeError("Failed to create asyncpg pool")
    return pool


def _stable_vec_from_text(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Deterministic unit vector for error payloads."""
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


class PointStruct(BaseModel):
    id: str
    vector: list[float] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    score: float | None = None

class QueryResponse(BaseModel):
    points: list[PointStruct]


def _is_ollama_embed_bad_request(e: BaseException) -> bool:
    try:
        import httpx

        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 400:
            return True
    except Exception:
        pass
    msg = str(e).lower()
    return "400" in msg or "bad request" in msg or "status code 400" in msg


async def _ollama_embed_query_robust(
    ollama: Any,
    query: str,
    *,
    embed_model: str,
    embed_model_fallback: str | None,
    keep_alive: str,
    query_max_chars: int,
) -> list[float]:
    """Truncate + tiered retry on 400; optional fallback model (cùng dim với index)."""
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
                emb_resp = await ollama.embed(
                    model=model,
                    input=chunk,
                    keep_alive=keep_alive,
                )
                return _embedding_vector_from_ollama_response(emb_resp)
            except Exception as e:
                last_err = e
                if _is_ollama_embed_bad_request(e):
                    logger.warning(
                        "event=rag_ollama_embed_failed model=%s tier_tokens=%s ollama_400_retry err=%s",
                        model,
                        tier,
                        str(e)[:220],
                    )
                    continue
                logger.warning("event=rag_ollama_embed_failed model=%s err=%s", model, e)
                raise RuntimeError(f"rag_ollama_embed_failed:{e!s}") from e
    raise RuntimeError(f"rag_ollama_embed_failed:{last_err!s}") from last_err


def _embedding_vector_from_ollama_response(resp: dict[str, Any]) -> list[float]:
    if "embedding" in resp:
        emb = resp["embedding"]
        return list(emb) if isinstance(emb, list) else list(emb or [])
    embs = resp.get("embeddings")
    if isinstance(embs, list) and embs:
        e0 = embs[0]
        return list(e0) if isinstance(e0, list) else list(e0 or [])
    raise ValueError("embed response missing embedding(s)")


def _before_retry_log(retry_state):
    logger.warning(
        f"CNPG Failover/Network issue detected. Retrying SQL execution... (Attempt: {retry_state.attempt_number})",
        exc_info=retry_state.outcome.exception(),
    )


class PGVectorStore:
    """Postgres HA vector store (pgvector) for SOP / ledger / experience."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def close(self) -> None:
        await self._pool.close()

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type(
            (asyncpg.PostgresConnectionError, asyncpg.CannotConnectNowError, asyncpg.ConnectionDoesNotExistError)
        ),
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
    ) -> QueryResponse:
        sql = """
        SELECT id, payload, 1 - (embedding <=> $1::vector) AS score
        FROM rag_documents
        WHERE collection_name = $2
        """
        params: list[Any] = [query, collection_name]
        param_idx = 3
        if payload_filters:
            for key, val in payload_filters.items():
                if val is None or not isinstance(val, str):
                    continue
                if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", str(key)):
                    continue
                sql += f" AND payload->>'{key}' = ${param_idx}"
                params.append(val)
                param_idx += 1
        # score_threshold: cosine similarity
        if score_threshold is not None:
            sql += f" AND 1 - (embedding <=> $1::vector) >= {float(score_threshold)}"

        sql += f" ORDER BY embedding <=> $1::vector LIMIT {int(limit)}"

        async with self._pool.acquire() as conn:
            # asyncpg + pgvector: bind Python list[float], not JSON string (breaks vector codec).
            rows = await conn.fetch(sql, *params)

        pts = []
        for r in rows:
            payload_data = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
            pts.append(
                PointStruct(
                    id=str(r["id"]),
                    payload=payload_data if with_payload else {},
                    score=float(r["score"]),
                )
            )
        return QueryResponse(points=pts)

    async def similarity_search(
        self,
        query: str,
        collection_id: str,
        *,
        ollama: Any,
        embed_model: str,
        embed_model_fallback: str | None = None,
        keep_alive: str = "5m",
        limit: int = 8,
        score_threshold: float | None = None,
        query_max_chars: int = 8000,
        payload_filters: dict[str, str] | None = None,
    ) -> QueryResponse:
        """
        Analyst / tools: embed ``query`` rồi cosine search trên ``rag_documents`` (cột ``collection_name`` = ``collection_id``).
        Không hardcode collection — truyền ``collection_id`` từ WorkerSettings.
        """
        if ollama is None:
            raise TypeError("similarity_search requires ollama client")
        vector = await _ollama_embed_query_robust(
            ollama,
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
            )
        except Exception as e:
            logger.warning("event=rag_pgvector_query_failed err=%s", e)
            raise RuntimeError(f"rag_pgvector_query_failed:{e!s}") from e

    def _rrf_merge_points(
        self,
        dense: list[PointStruct],
        sparse: list[PointStruct],
        *,
        k: int = 60,
        dense_weight: float = 0.65,
    ) -> list[PointStruct]:
        """Reciprocal rank fusion for hybrid retrieval."""
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
        out: list[PointStruct] = []
        for pid in ordered:
            out.append(
                PointStruct(
                    id=pid,
                    payload=payloads.get(pid, {}),
                    score=float(scores[pid]),
                )
            )
        return out

    async def fulltext_search_points(
        self,
        collection_name: str,
        query_text: str,
        limit: int = 12,
    ) -> QueryResponse:
        """BM25-style ranking via Postgres ``ts_rank`` + ``plainto_tsquery``."""
        qt = (query_text or "").strip()
        if len(qt) < 2:
            return QueryResponse(points=[])
        # Avoid tsquery parse errors: keep alnum-ish words
        safe = re.sub(r"[^\w\s.-]", " ", qt)[:2000].strip()
        if len(safe) < 2:
            return QueryResponse(points=[])
        sql = """
        SELECT id, payload,
          ts_rank(
            to_tsvector('english', left(
              coalesce(payload->>'text','') || ' ' || coalesce(payload->>'summary',''),
              12000
            )),
            plainto_tsquery('english', $2)
          ) AS score
        FROM rag_documents
        WHERE collection_name = $1
          AND to_tsvector('english', left(
              coalesce(payload->>'text','') || ' ' || coalesce(payload->>'summary',''),
              12000
            )) @@ plainto_tsquery('english', $2)
        ORDER BY score DESC
        LIMIT $3
        """
        lim = max(1, min(48, int(limit)))
        async with self._pool.acquire() as conn:
            try:
                rows = await conn.fetch(sql, collection_name, safe, lim)
            except Exception as e:
                logger.warning("fulltext_search_points failed: %s", e)
                return QueryResponse(points=[])
        pts: list[PointStruct] = []
        for r in rows:
            payload_data = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
            sc = float(r["score"] or 0.0)
            pts.append(PointStruct(id=str(r["id"]), payload=payload_data, score=sc))
        return QueryResponse(points=pts)

    async def similarity_search_hybrid(
        self,
        query: str,
        collection_id: str,
        *,
        ollama: Any,
        embed_model: str,
        embed_model_fallback: str | None = None,
        keep_alive: str = "5m",
        limit: int = 8,
        score_threshold: float | None = None,
        query_max_chars: int = 8000,
        payload_filters: dict[str, str] | None = None,
        hybrid_vector_weight: float = 0.65,
    ) -> QueryResponse:
        """Dense + sparse merge (RRF), then apply score_threshold to best dense score per id."""
        if ollama is None:
            raise TypeError("similarity_search_hybrid requires ollama client")
        vector = await _ollama_embed_query_robust(
            ollama,
            query,
            embed_model=embed_model,
            embed_model_fallback=embed_model_fallback,
            keep_alive=keep_alive,
            query_max_chars=int(query_max_chars),
        )
        dense_lim = max(limit * 3, 16)
        try:
            dense = await self.query_points(
                collection_name=collection_id,
                query=vector,
                limit=int(dense_lim),
                score_threshold=None,
                with_payload=True,
                payload_filters=payload_filters,
            )
        except Exception as e:
            logger.warning("event=rag_pgvector_query_failed err=%s", e)
            raise RuntimeError(f"rag_pgvector_query_failed:{e!s}") from e
        sparse = await self.fulltext_search_points(
            collection_id,
            (query or "")[: int(query_max_chars)],
            limit=max(12, int(limit) * 3),
        )
        merged = self._rrf_merge_points(
            list(dense.points or []),
            list(sparse.points or []),
            dense_weight=float(hybrid_vector_weight),
        )
        # Re-fetch dense scores for threshold; keep merge order for top `limit`
        dense_by_id = {str(p.id): float(p.score or 0.0) for p in (dense.points or [])}
        out_pts: list[PointStruct] = []
        for p in merged:
            did = str(p.id)
            best_sim = dense_by_id.get(did)
            if best_sim is None:
                # id only from sparse: run single id lookup optional — skip low-confidence
                continue
            if score_threshold is not None and best_sim < float(score_threshold):
                continue
            out_pts.append(
                PointStruct(
                    id=did,
                    payload=dict(p.payload or {}),
                    score=best_sim,
                )
            )
            if len(out_pts) >= int(limit):
                break
        if not out_pts and dense.points:
            # Fallback: pure dense if hybrid produced nothing
            for p in dense.points[: int(limit)]:
                sc = float(getattr(p, "score", None) or 0.0)
                if score_threshold is not None and sc < float(score_threshold):
                    continue
                out_pts.append(p)
                if len(out_pts) >= int(limit):
                    break
        return QueryResponse(points=out_pts)

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type(
            (asyncpg.PostgresConnectionError, asyncpg.CannotConnectNowError, asyncpg.ConnectionDoesNotExistError)
        ),
        reraise=True,
        before_sleep=_before_retry_log,
    )
    async def upsert(self, collection_name: str, points: list[Any]) -> None:
        sql = """
        INSERT INTO rag_documents (id, collection_name, embedding, payload)
        VALUES ($1::uuid, $2, $3::vector, $4::jsonb)
        ON CONFLICT (collection_name, id) DO UPDATE 
        SET embedding = EXCLUDED.embedding, payload = EXCLUDED.payload;
        """
        data = []
        for p in points:
            # support dict or PointStruct
            pid = getattr(p, "id", None) or p["id"]
            pvec = getattr(p, "vector", None) or p["vector"]
            ppayload = getattr(p, "payload", None) or (p.get("payload", {}) if isinstance(p, dict) else {})
            data.append((pid, collection_name, pvec, json.dumps(ppayload)))

        async with self._pool.acquire() as conn:
            await conn.executemany(sql, data)

    async def ensure_ready(self) -> None:
        """Create extension and tables if not exist."""
        if getattr(self, "_initialized", False):
            return

        sql = """
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE TABLE IF NOT EXISTS rag_documents (
            id UUID,
            collection_name VARCHAR(64) NOT NULL,
            embedding vector(768),
            payload JSONB DEFAULT '{}'::jsonb,
            PRIMARY KEY (collection_name, id)
        ) PARTITION BY LIST (collection_name);
        
        CREATE TABLE IF NOT EXISTS doc_sop PARTITION OF rag_documents FOR VALUES IN ('itops_sop_ledger');
        CREATE TABLE IF NOT EXISTS doc_sop_v2 PARTITION OF rag_documents FOR VALUES IN ('itops_sop_ledger_v2');
        CREATE TABLE IF NOT EXISTS doc_errors PARTITION OF rag_documents FOR VALUES IN ('itops_error_ledger');
        CREATE TABLE IF NOT EXISTS doc_infra PARTITION OF rag_documents FOR VALUES IN ('infra_topology');
        CREATE TABLE IF NOT EXISTS doc_action PARTITION OF rag_documents FOR VALUES IN ('action_experience');
        CREATE TABLE IF NOT EXISTS doc_cli PARTITION OF rag_documents FOR VALUES IN ('cli_hil_context');
        CREATE TABLE IF NOT EXISTS doc_vendor PARTITION OF rag_documents FOR VALUES IN ('vendor_knowledge');
        CREATE TABLE IF NOT EXISTS doc_sre_knowledge PARTITION OF rag_documents FOR VALUES IN ('SRE_KNOWLEDGE');
        CREATE TABLE IF NOT EXISTS doc_k8s_expert PARTITION OF rag_documents FOR VALUES IN ('k8s_expert');
        
        CREATE INDEX IF NOT EXISTS doc_sop_embedding_idx ON doc_sop USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS doc_sop_v2_embedding_idx ON doc_sop_v2 USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS doc_errors_embedding_idx ON doc_errors USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS doc_vendor_embedding_idx ON doc_vendor USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
        CREATE INDEX IF NOT EXISTS doc_sre_knowledge_embedding_idx ON doc_sre_knowledge USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS doc_k8s_expert_embedding_idx ON doc_k8s_expert USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS doc_k8s_expert_fts_idx ON doc_k8s_expert USING gin (
          to_tsvector('english', left(
            coalesce(payload->>'text','') || ' ' || coalesce(payload->>'summary',''),
            12000
          ))
        );
        """
        async with self._pool.acquire() as conn:
            await conn.execute(sql)
            self._initialized = True

    async def ensure_partition_for_collection(self, collection_name: str) -> None:
        """Tạo LIST partition + HNSW nếu env đổi tên collection (chỉ [a-zA-Z0-9_])."""
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]{0,62}$", str(collection_name)):
            raise ValueError(f"invalid collection_name for partition: {collection_name!r}")
        cn = str(collection_name)
        suffix = re.sub(r"[^a-zA-Z0-9_]", "_", cn).lower()[:48]
        ident = f"doc_auto_{suffix}"
        idx = f"{ident}_embedding_idx"
        sql = f"""
        CREATE TABLE IF NOT EXISTS {ident} PARTITION OF rag_documents FOR VALUES IN ('{cn.replace("'", "''")}');
        CREATE INDEX IF NOT EXISTS {idx} ON {ident} USING hnsw (embedding vector_cosine_ops);
        """
        async with self._pool.acquire() as conn:
            await conn.execute(sql)

    async def action_experience_unique_counts(self) -> dict[str, int]:
        """
        Approx governance counters from JSON payload in action_experience:
        - unique_success_patterns
        - unique_fail_patterns
        """
        sql = """
        SELECT
          COUNT(DISTINCT CASE
            WHEN (payload->>'exec_outcome') = 'success' THEN COALESCE(payload->>'pattern_key', id::text)
            ELSE NULL
          END) AS unique_success_patterns,
          COUNT(DISTINCT CASE
            WHEN (payload->>'exec_outcome') <> 'success' THEN COALESCE(payload->>'pattern_key', id::text)
            ELSE NULL
          END) AS unique_fail_patterns
        FROM rag_documents
        WHERE collection_name = $1
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(sql, COLLECTION_ACTION_EXPERIENCE)
        if not row:
            return {"unique_success_patterns": 0, "unique_fail_patterns": 0}
        return {
            "unique_success_patterns": int(row["unique_success_patterns"] or 0),
            "unique_fail_patterns": int(row["unique_fail_patterns"] or 0),
        }


# Thay cho log_error_to_ledger cũ
async def log_error_to_ledger(
    client: PGVectorStore | asyncpg.Pool,
    *,
    title: str,
    detail: str,
    phase: str,
    component: str = "",
    collection_name: str = COLLECTION_ERRORS,
    extra: dict[str, Any] | None = None,
) -> str:
    payload_text = f"{title}\n{detail}::{phase}::{component}"
    vector = _stable_vec_from_text(payload_text)
    point_id_str = str(uuid.uuid5(uuid.NAMESPACE_URL, payload_text + str(datetime.now(UTC).date())))

    payload: dict[str, Any] = {
        "title": title,
        "detail": detail,
        "phase": phase,
        "component": component,
        "ts": datetime.now(UTC).isoformat(),
    }
    if extra:
        payload["extra"] = extra
        
    store = client if isinstance(client, PGVectorStore) else PGVectorStore(client)
    await store.upsert(
        collection_name=collection_name,
        points=[PointStruct(id=point_id_str, vector=vector, payload=payload)],
    )
    return point_id_str
