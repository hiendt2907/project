"""Semantic cache backed by Redis Stack (FT KNN) — wraps RAG query results with TTL."""

from __future__ import annotations

import json
import logging
import struct
import time
import uuid
from typing import Any

import redis.asyncio as aioredis
from redis.commands.search.field import NumericField, TagField, VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query

from rag.redis_vector_store import (
    DEFAULT_TENANT_ID,
    EMBED_DIM,
    QueryResponse,
    _ft_escape,
    validate_tenant_id,
)

logger = logging.getLogger(__name__)

_SEMCACHE_IDX = "idx:semcache"
_SEMCACHE_PREFIX = "semcache:"


class SemanticCache:
    """
    Store recent RAG query results keyed by embedding vector.
    On cache hit (cosine similarity >= threshold), returns cached QueryResponse
    without hitting Ollama embed + Redis FT search again.
    """

    def __init__(self, r: aioredis.Redis, *, default_ttl_sec: int = 3600) -> None:
        self._r = r
        self._default_ttl = default_ttl_sec
        self._ready = False

    async def ensure_ready(self) -> None:
        if self._ready:
            return
        try:
            info = await self._r.ft(_SEMCACHE_IDX).info()
            attrs = info.get("attributes") if isinstance(info, dict) else None
            has_tenant_field = bool(attrs) and any("tenant_id" in str(a) for a in attrs)
            if has_tenant_field:
                self._ready = True
                return
        except Exception:
            pass  # index missing entirely — fall through to create_index below
        else:
            # Legacy index predates per-tenant isolation. Entries are TTL'd cache
            # data (no durability requirement) — drop and recreate with the
            # tenant_id tag field instead of migrating individual docs.
            try:
                await self._r.ft(_SEMCACHE_IDX).dropindex()
            except Exception as e:
                logger.warning("event=semcache_legacy_index_drop_failed err=%s", e)
        try:
            await self._r.ft(_SEMCACHE_IDX).create_index(
                [
                    VectorField(
                        "$.embedding",
                        "HNSW",
                        {
                            "TYPE": "FLOAT32",
                            "DIM": str(EMBED_DIM),
                            "DISTANCE_METRIC": "COSINE",
                            "INITIAL_CAP": "1000",
                            "M": "16",
                            "EF_CONSTRUCTION": "64",
                        },
                        as_name="embedding",
                    ),
                    NumericField("$.ts", as_name="ts", sortable=True),
                    TagField("$.tenant_id", as_name="tenant_id"),
                ],
                definition=IndexDefinition(
                    prefix=[_SEMCACHE_PREFIX], index_type=IndexType.JSON
                ),
            )
            self._ready = True
        except Exception as e:
            logger.warning("event=semcache_index_create_failed err=%s", e)

    async def get(
        self,
        vec: list[float],
        *,
        threshold: float = 0.95,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> QueryResponse | None:
        if not self._ready:
            await self.ensure_ready()
        try:
            tid = _ft_escape(validate_tenant_id(tenant_id))
            vec_bytes = struct.pack(f"{EMBED_DIM}f", *vec)
            q = (
                Query(f"(@tenant_id:{{{tid}}})=>[KNN 1 @embedding $vec AS __score]")
                .sort_by("__score", asc=True)
                .return_fields("result_json", "__score")
                .paging(0, 1)
                .dialect(2)
            )
            results = await self._r.ft(_SEMCACHE_IDX).search(
                q, query_params={"vec": vec_bytes}
            )
            if not results.docs:
                return None
            doc = results.docs[0]
            distance = float(getattr(doc, "__score", 1.0))
            similarity = 1.0 - distance
            if similarity < threshold:
                return None
            result_json = getattr(doc, "result_json", None)
            if not result_json:
                return None
            return QueryResponse.model_validate_json(result_json)
        except Exception as e:
            logger.debug("event=semcache_get_failed err=%s", e)
            return None

    async def set(
        self,
        vec: list[float],
        result: QueryResponse,
        *,
        ttl_sec: int | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        if not self._ready:
            await self.ensure_ready()
        tid = validate_tenant_id(tenant_id)
        ttl = ttl_sec if ttl_sec is not None else self._default_ttl
        try:
            key = f"{_SEMCACHE_PREFIX}{uuid.uuid4()}"
            doc = {
                "embedding": list(vec),
                "result_json": result.model_dump_json(),
                "ts": time.time(),
                "tenant_id": tid,
            }
            await self._r.json().set(key, "$", doc)
            await self._r.expire(key, ttl)
        except Exception as e:
            logger.debug("event=semcache_set_failed err=%s", e)
