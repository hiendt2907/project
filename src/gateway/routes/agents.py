"""Agents route — read worker heartbeats from Redis (written by observability_metrics_loop)."""
from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/agents", tags=["agents"])

_HB_PREFIX = "omni:agent:heartbeat:"
_STALE_SEC = 90  # heartbeat older than 90s → stale


def _get_redis(request: Request) -> Any:
    r = getattr(request.app.state, "redis", None)
    if r is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return r


@router.get("")
async def list_agents(request: Request) -> JSONResponse:
    """Return current health of all known worker roles."""
    redis = _get_redis(request)
    now = int(time.time())

    try:
        keys = await redis.keys(f"{_HB_PREFIX}*")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis error: {e}") from e

    agents: list[dict] = []
    for key in sorted(keys):
        raw = await redis.get(key)
        if not raw:
            continue
        try:
            hb = json.loads(raw)
        except Exception:
            continue

        age = now - int(hb.get("updated_at", 0))
        hb["age_seconds"] = age
        hb["stale"] = age > _STALE_SEC
        if hb["stale"]:
            hb["status"] = "stale"
        agents.append(hb)

    overall = "ok"
    for a in agents:
        if a.get("status") == "unhealthy":
            overall = "unhealthy"
            break
        if a.get("status") in ("degraded", "stale"):
            overall = "degraded"

    return JSONResponse(content={
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "overall": overall,
        "count": len(agents),
        "agents": agents,
    })
