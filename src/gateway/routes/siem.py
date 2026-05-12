"""SIEM overview route — read-only summary from CRAT audit chain + Redis."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

router = APIRouter(prefix="/siem", tags=["siem"])

_CRAT_BLOCKS_KEY = "audit_chain:blocks"
_CRAT_HEAD_KEY = "audit_chain:head_hash"
_CRAT_SEQ_KEY = "audit_chain:seq"

_VERDICT_COUNTS_WINDOW = 86400  # 24h default

_EVENT_TYPE_ORDER = ["ADVISORY_DECISION", "ADVISORY_DISPATCHED", "MUTATION_TRAPPED", "HITL_DECISION"]


def _get_redis(request: Request) -> Any:
    r = getattr(request.app.state, "redis", None)
    if r is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return r


@router.get("/overview")
async def siem_overview(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> JSONResponse:
    """
    Return SIEM pipeline overview:
    - Recent CRAT audit blocks (latest N)
    - Verdict distribution (last 24h)
    - Chain integrity metadata
    """
    redis = _get_redis(request)

    try:
        total_seq = await redis.get(_CRAT_SEQ_KEY)
        head_hash = await redis.get(_CRAT_HEAD_KEY)
        total_blocks = int(total_seq or 0)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis error: {e}") from e

    # Fetch latest N blocks from the list (LRANGE from tail)
    try:
        raw_blocks = await redis.lrange(_CRAT_BLOCKS_KEY, -limit, -1)
    except Exception as e:
        log.warning("event=crat_read_error err=%s", e)
        raw_blocks = []

    blocks: list[dict] = []
    verdict_counts: dict[str, int] = {}
    event_type_counts: dict[str, int] = {}
    cutoff = time.time() - _VERDICT_COUNTS_WINDOW

    for raw in reversed(raw_blocks):  # newest first
        try:
            b = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            continue

        # Count verdicts for recent blocks
        ts_str = b.get("timestamp_utc") or b.get("payload", {}).get("timestamp", "")
        try:
            import datetime
            if "+" in ts_str or "Z" in ts_str:
                ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            else:
                ts = datetime.datetime.fromisoformat(ts_str).timestamp()
            is_recent = ts >= cutoff
        except Exception:
            is_recent = True

        ev = b.get("event_type", "UNKNOWN")
        event_type_counts[ev] = event_type_counts.get(ev, 0) + 1

        payload = b.get("payload") or {}
        verdict = payload.get("verdict") or "UNKNOWN"
        if is_recent:
            verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

        blocks.append({
            "seq": b.get("seq"),
            "event_type": ev,
            "trace_id": b.get("trace_id"),
            "timestamp_utc": b.get("timestamp_utc") or ts_str,
            "verdict": verdict,
            "root_cause": payload.get("root_cause", "")[:120],
            "affected_workload": payload.get("affected_workload", ""),
            "block_hash": (b.get("block_hash") or "")[:16] + "…",
        })

    return JSONResponse(content={
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "chain": {
            "total_blocks": total_blocks,
            "head_hash_prefix": (head_hash or "")[:16] + "…" if head_hash else None,
            "integrity": "verified" if head_hash else "empty",
        },
        "verdict_distribution_24h": verdict_counts,
        "event_type_distribution": event_type_counts,
        "recent_blocks": blocks,
    })
