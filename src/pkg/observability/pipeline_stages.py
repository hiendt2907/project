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
    "VERIFY",
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
_LOGS_KEY_PREFIX = "omni:trace:logs:"
_TTL_SEC = 3600
_STREAM_MAXLEN = 2000
_LOGS_MAXLEN = 400


async def append_trace_log(
    redis: Any,
    trace_id: str,
    phase: str,
    line: str,
    *,
    level: str = "info",
) -> None:
    """Append one raw log line for a trace/phase to omni:trace:logs:{trace}.

    Stored as a capped Redis LIST of JSON ``{ts, phase, level, line}`` (newest last
    via RPUSH + left-trim). Best-effort: swallows Redis errors. Lets the UI render a
    per-phase log stream alongside the pipeline flow.
    """
    if not trace_id or len(trace_id) > 128 or not line:
        return
    key = f"{_LOGS_KEY_PREFIX}{trace_id}"
    entry = json.dumps(
        {"ts": time.time(), "phase": str(phase or "")[:32], "level": str(level or "info")[:12], "line": str(line)[:600]},
        ensure_ascii=False,
    )
    try:
        await redis.rpush(key, entry)
        await redis.ltrim(key, -_LOGS_MAXLEN, -1)
        await redis.expire(key, _TTL_SEC)
    except Exception as exc:  # noqa: BLE001 — logs are best-effort
        log.debug("pipeline_stages: append_trace_log redis error trace=%s err=%s", trace_id, exc)


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
        # Every stage transition is also a per-phase log line (free per-phase log
        # stream at all mark_stage call sites). Level maps status → info/warn.
        _level = "error" if status == "fail" else ("warn" if status == "skip" else "info")
        _logline = f"stage {stage} → {status}" + (f": {detail}" if detail else "")
        await append_trace_log(redis, trace_id, stage, _logline, level=_level)
    except Exception as exc:
        log.warning(
            "pipeline_stages: redis error stage=%s trace=%s status=%s err=%s",
            stage,
            trace_id,
            status,
            exc,
        )
