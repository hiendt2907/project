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
        emb_resp = await ollama.embed(
            model=embed_model,
            input=(query or "")[: int(query_max_chars)],
            keep_alive=keep_alive,
        )
        vector = _embedding_vector_from_ollama_response(emb_resp)
        return await self.query_points(
            collection_name=collection_id,
            query=vector,
            limit=int(limit),
            score_threshold=score_threshold,
            with_payload=True,
            payload_filters=payload_filters,
        )

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
