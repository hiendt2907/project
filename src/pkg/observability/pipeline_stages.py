"""Per-trace pipeline stage tracker (shared gateway + workers).

Canonical home for PIPELINE_STAGES + mark_stage. Lives under src/pkg/ so BOTH
the gateway image and the worker image can import the same source — no drift,
and the gateway can mark stages (e.g. INGEST) without importing workers/.

Stdlib-only (json/time/logging); the redis client is passed in by the caller.
All writes are best-effort: Redis errors are logged but never propagate.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

PIPELINE_STAGES: list[str] = [
    "INGEST",
    "EVIDENCE",
    "RAG",
    "LLM",
    "SCHEMA",
    "KILLSWITCH",
    "CRAT",
    "DISPATCH",
    "HITL",
    "EXECUTOR",
    "FEEDBACK",
]

_VALID_STATUSES = frozenset({"ok", "fail", "skip", "pending"})
_KEY_PREFIX = "omni:trace:stages:"
_EVENTS_STREAM = "omni:trace:events"
_TTL_SEC = 3600
_STREAM_MAXLEN = 2000


async def mark_stage(
    redis: Any,
    trace_id: str,
    stage: str,
    status: str = "ok",
    *,
    detail: str = "",
    lane: str = "",
) -> None:
    """Record a pipeline stage transition for trace_id.

    Best-effort: swallows Redis errors with a warning log.
    Validates inputs silently — invalid stage or trace_id is a no-op.

    ``lane`` uses last-non-empty-wins: an empty lane never clears an existing
    one, so callers that learn the lane late (after resolve_proof_lane) can
    enrich the trace meta without earlier empty marks wiping it.
    """
    if not trace_id or len(trace_id) > 128:
        log.debug("pipeline_stages: skipping invalid trace_id len=%s", len(trace_id or ""))
        return
    if stage not in PIPELINE_STAGES:
        log.debug("pipeline_stages: unknown stage=%s trace=%s", stage, trace_id)
        return
    if status not in _VALID_STATUSES:
        status = "ok"

    ts = time.time()
    key = f"{_KEY_PREFIX}{trace_id}"

    try:
        # Build stage entry (immutable dict copy — never mutate arg)
        stage_entry: dict[str, Any] = {
            "status": status,
            "ts": ts,
            "detail": detail,
        }

        # Read existing meta, keep first started_at
        try:
            raw_meta = await redis.hget(key, "__meta__")
            existing_meta: dict[str, Any] = json.loads(raw_meta) if raw_meta else {}
        except Exception:
            existing_meta = {}

        new_meta: dict[str, Any] = {
            **existing_meta,
            "lane": lane or existing_meta.get("lane", ""),
            "trace_id": trace_id,
            "updated_at": ts,
        }
        if "started_at" not in new_meta:
            new_meta = {**new_meta, "started_at": ts}

        await redis.hset(key, stage, json.dumps(stage_entry))
        await redis.hset(key, "__meta__", json.dumps(new_meta))
        await redis.expire(key, _TTL_SEC)

        # Publish to global event stream for SSE consumers
        await redis.xadd(
            _EVENTS_STREAM,
            {
                "trace_id": trace_id,
                "stage": stage,
                "status": status,
                "lane": lane,
                "ts": str(ts),
            },
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as exc:
        log.warning(
            "pipeline_stages: redis error stage=%s trace=%s status=%s err=%s",
            stage,
            trace_id,
            status,
            exc,
        )
