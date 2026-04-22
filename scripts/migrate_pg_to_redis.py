#!/usr/bin/env python3
"""
Migrate RAG vectors + payloads từ Postgres/pgvector → Redis Stack.

Chạy một lần trước khi switch traffic sang RedisVectorStore.
Yêu cầu: cả Postgres (cũ) và Redis Stack (mới) đều phải đang chạy.

Usage (set env vars, never hardcode credentials):
  export PG_DSN="postgresql://appuser:<password>@localhost:5432/ragdb"
  export OMNI_REDIS_URL="redis://localhost:6379/0"
  POSTGRES_RAG_DSN="$PG_DSN" .venv/bin/python scripts/migrate_pg_to_redis.py [--collection k8s_expert] [--batch 200] [--dry-run]

Port-forward nếu cần:
  kubectl port-forward svc/pgpool-gateway 5432:5432 -n multi-agent &
  kubectl port-forward svc/redis 6379:6379 -n multi-agent &
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import struct
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

EMBED_DIM = 768
DEFAULT_BATCH = 200


async def ensure_ft_index(r: Any, collection: str) -> None:
    """Create FT index for collection if it doesn't exist."""
    from redis.commands.search.field import TagField, TextField, VectorField
    from redis.commands.search.index_definition import IndexDefinition, IndexType

    idx = f"idx:{collection}"
    try:
        await r.ft(idx).info()
        logger.info("index %s already exists", idx)
        return
    except Exception:
        pass

    fields = [
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
    await r.ft(idx).create_index(
        fields,
        definition=IndexDefinition(
            prefix=[f"doc:{collection}:"], index_type=IndexType.HASH
        ),
    )
    logger.info("created FT index %s", idx)


async def migrate_collection(
    pg_pool: Any,
    r: Any,
    collection: str,
    batch_size: int,
    dry_run: bool,
) -> int:
    """Migrate one collection from Postgres → Redis. Returns total migrated."""
    await ensure_ft_index(r, collection)

    offset = 0
    total = 0

    while True:
        rows = await pg_pool.fetch(
            """
            SELECT id::text, embedding, payload
            FROM rag_documents
            WHERE collection_name = $1
            ORDER BY id
            LIMIT $2 OFFSET $3
            """,
            collection,
            batch_size,
            offset,
        )
        if not rows:
            break

        if not dry_run:
            pipe = r.pipeline(transaction=False)
            for row in rows:
                doc_id = str(row["id"])
                payload: dict[str, Any] = (
                    json.loads(row["payload"])
                    if isinstance(row["payload"], str)
                    else dict(row["payload"] or {})
                )
                emb: list[float] = list(row["embedding"])
                if len(emb) != EMBED_DIM:
                    logger.warning(
                        "skip doc %s: dim=%d expected %d", doc_id, len(emb), EMBED_DIM
                    )
                    continue

                vec_bytes = struct.pack(f"{EMBED_DIM}f", *emb)
                text_content = (
                    str(payload.get("text", ""))
                    + " "
                    + str(payload.get("summary", ""))
                )[:4000]
                key = f"doc:{collection}:{doc_id}"
                pipe.hset(
                    key,
                    mapping={
                        "embedding": vec_bytes,
                        "payload": json.dumps(payload, ensure_ascii=False),
                        "text_content": text_content,
                        "source": str(payload.get("source", "")),
                        "doc_type": str(payload.get("type", "")),
                    },
                )
            await pipe.execute()

        total += len(rows)
        offset += batch_size
        logger.info("collection=%-30s migrated=%d", collection, total)

    logger.info("DONE collection=%-30s total=%d dry_run=%s", collection, total, dry_run)
    return total


async def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate pgvector → Redis Stack")
    parser.add_argument("--collection", default="", help="Specific collection or empty for all")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--dry-run", action="store_true", help="Count rows without writing")
    args = parser.parse_args()

    pg_dsn = os.environ.get("POSTGRES_RAG_DSN", "")
    redis_url = os.environ.get("OMNI_REDIS_URL", "redis://redis:6379/0")

    if not pg_dsn:
        logger.error("POSTGRES_RAG_DSN env var required")
        sys.exit(1)

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

    import asyncpg
    import redis.asyncio as aioredis

    try:
        from pgvector.asyncpg import register_vector
    except ImportError:
        logger.warning("pgvector not installed — embedding column will be bytes, not list[float]")
        register_vector = None

    async def _init_conn(conn: asyncpg.Connection) -> None:
        if register_vector:
            await register_vector(conn)

    logger.info("connecting Postgres DSN=%s...", pg_dsn[:40])
    pg_pool = await asyncpg.create_pool(dsn=pg_dsn, init=_init_conn, min_size=2, max_size=8)

    logger.info("connecting Redis URL=%s...", redis_url)
    r = aioredis.from_url(redis_url, decode_responses=False)

    # Ping both
    await r.ping()
    await pg_pool.fetchval("SELECT 1")
    logger.info("connections OK")

    if args.collection:
        collections = [args.collection]
    else:
        rows = await pg_pool.fetch(
            "SELECT DISTINCT collection_name FROM rag_documents ORDER BY collection_name"
        )
        collections = [row["collection_name"] for row in rows]
        logger.info("found %d collections: %s", len(collections), collections)

    grand_total = 0
    for coll in collections:
        n = await migrate_collection(pg_pool, r, coll, args.batch, args.dry_run)
        grand_total += n

    logger.info("MIGRATION COMPLETE total_docs=%d dry_run=%s", grand_total, args.dry_run)
    await pg_pool.close()
    await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
