"""Embedding-based incident clustering (S3.1).

Groups related alerts by semantic similarity of their error hints so that
one root cause generating N downstream failures is recognised as a single incident.

Redis schema:
  omni:cluster:meta:{cluster_id}  → HSET: cluster_id, namespace, first_alert_fp,
                                          member_count, created_at, last_seen_at
  omni:cluster:centroid:{cluster_id} → JSON list[float] (rolling average embedding)

Collection: omni_incident_clusters  (Redis HNSW via vector_store)
Key TTL: 7 days (clusters auto-expire if no new members arrive).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_CLUSTER_SCORE_THRESHOLD = 0.82  # Cosine similarity for cluster membership
_CLUSTER_META_TTL = 86400 * 7    # 7 days
_CLUSTER_COLLECTION = "omni_incident_clusters"
_EMBED_DIM = 768                  # nomic-embed-text dimensions


async def assign_to_cluster(
    ctx: Any,
    *,
    alert_fp: str,
    error_hint: str,
    namespace: str,
) -> str:
    """Assign an alert to an existing cluster or create a new one.

    Returns the cluster_id for the alert.  Never raises — falls back to a
    unique cluster_id derived from alert_fp on any error.
    """
    try:
        return await _assign_impl(ctx, alert_fp=alert_fp, error_hint=error_hint, namespace=namespace)
    except Exception as e:
        logger.warning("event=cluster_assign_fallback alert_fp=%s err=%s", alert_fp, e)
        return f"cls-fallback-{alert_fp[:16]}"


async def _assign_impl(
    ctx: Any,
    *,
    alert_fp: str,
    error_hint: str,
    namespace: str,
) -> str:
    vs = getattr(ctx, "vector_store", None)
    llm = getattr(ctx, "llm", None)
    redis = getattr(ctx, "redis", None)
    ws = getattr(ctx, "settings", None)

    if vs is None or llm is None:
        return f"cls-nollm-{alert_fp[:16]}"

    embed_model = str(getattr(ws, "embed_model", "nomic-embed-text") or "nomic-embed-text")
    embed_resp = await llm.embed(model=embed_model, input=error_hint[:2000])
    raw = embed_resp.get("embedding") or (embed_resp.get("embeddings") or [[]])[0]
    embedding: list[float] = list(raw)
    if len(embedding) != _EMBED_DIM:
        embedding = (embedding + [0.0] * _EMBED_DIM)[:_EMBED_DIM]

    # Search existing clusters.
    try:
        result = await vs.similarity_search_raw(
            embedding,
            _CLUSTER_COLLECTION,
            limit=3,
            score_threshold=_CLUSTER_SCORE_THRESHOLD,
        )
        points = result.points if result else []
    except Exception:
        points = []

    if points:
        best = points[0]
        cluster_id = str(best.payload.get("cluster_id") or best.id)
        if redis:
            try:
                meta_key = f"omni:cluster:meta:{cluster_id}"
                await redis.hincrby(meta_key, "member_count", 1)
                await redis.hset(meta_key, "last_seen_at", str(time.time()))
                await redis.expire(meta_key, _CLUSTER_META_TTL)
            except Exception as _re:
                logger.debug("cluster meta update fail: %s", _re)
        await _update_centroid(vs, redis, cluster_id, embedding)
        logger.debug(
            "event=cluster_join cluster_id=%s alert_fp=%s score=%.3f",
            cluster_id, alert_fp, float(best.score or 0),
        )
        return cluster_id

    # Create new cluster.
    cluster_id = f"cls-{uuid.uuid4().hex[:8]}"
    try:
        from rag.pgvector_store import PointStruct
        await vs.upsert(
            collection_name=_CLUSTER_COLLECTION,
            points=[PointStruct(
                id=cluster_id,
                vector=embedding,
                payload={
                    "cluster_id": cluster_id,
                    "namespace": namespace,
                    "first_alert_fp": alert_fp,
                    "member_count": 1,
                    "created_at": str(time.time()),
                    "last_seen_at": str(time.time()),
                },
            )],
        )
    except Exception as e:
        logger.warning("event=cluster_upsert_fail cluster_id=%s err=%s", cluster_id, e)

    if redis:
        try:
            meta_key = f"omni:cluster:meta:{cluster_id}"
            await redis.hset(meta_key, mapping={
                "cluster_id": cluster_id,
                "namespace": namespace,
                "first_alert_fp": alert_fp,
                "member_count": "1",
                "created_at": str(time.time()),
                "last_seen_at": str(time.time()),
            })
            await redis.expire(meta_key, _CLUSTER_META_TTL)
        except Exception as _re:
            logger.debug("cluster meta create fail: %s", _re)

    logger.info(
        "event=cluster_created cluster_id=%s alert_fp=%s namespace=%s",
        cluster_id, alert_fp, namespace,
    )
    return cluster_id


async def _update_centroid(vs: Any, redis: Any, cluster_id: str, new_embedding: list[float]) -> None:
    """Rolling average centroid update (exponential moving average, alpha=0.1)."""
    if vs is None:
        return
    alpha = 0.1
    try:
        result = await vs.similarity_search_raw(
            new_embedding,
            _CLUSTER_COLLECTION,
            limit=1,
            score_threshold=0.0,
        )
        if not result or not result.points:
            return
        existing = result.points[0]
        old_vec = existing.vector if hasattr(existing, "vector") and existing.vector else None
        if old_vec is None or len(old_vec) != _EMBED_DIM:
            return
        updated = [alpha * n + (1 - alpha) * o for n, o in zip(new_embedding, old_vec)]
        from rag.pgvector_store import PointStruct
        await vs.upsert(
            collection_name=_CLUSTER_COLLECTION,
            points=[PointStruct(
                id=cluster_id,
                vector=updated,
                payload=existing.payload or {},
            )],
        )
    except Exception as e:
        logger.debug("cluster centroid update fail: %s", e)
