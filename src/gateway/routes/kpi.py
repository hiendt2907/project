"""KPI summary and trend endpoints — read-only, no pipeline coupling."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/kpi", tags=["kpi"])

_WINDOW_MAP = {
    "1h": 3600,
    "6h": 21600,
    "24h": 86400,
    "7d": 604800,
}


def _get_redis(request: Request) -> Any:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return redis


_WINDOW_SECONDS = 86400


async def _fetch_kpi_summary(redis: Any) -> dict:
    now = time.time()
    since = now - _WINDOW_SECONDS
    accepted = int(await redis.zcount("omni:kpi:z:accepted", since, "+inf") or 0)
    rejected = int(await redis.zcount("omni:kpi:z:rejected", since, "+inf") or 0)
    false_pos = int(await redis.zcount("omni:kpi:z:false_positive", since, "+inf") or 0)
    total_advisory = accepted + rejected
    total_executed = accepted

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window": "24h",
        "source": "redis",
        "advisory": {
            "accepted": accepted,
            "rejected": rejected,
            "total": total_advisory,
            "acceptance_rate": round(accepted / total_advisory, 4) if total_advisory else None,
        },
        "execution": {
            "total_executed": total_executed,
            "false_positive": false_pos,
            "false_positive_rate": round(false_pos / total_executed, 4) if total_executed else None,
        },
    }


async def _fetch_kpi_trend(redis: Any, window_seconds: int) -> dict:
    now = time.time()
    since = now - window_seconds
    lanes = ["SYS_RESOURCE", "SYS_HARD_FAIL", "APP_HTTP", "SIEM_SECURITY"]
    trend: dict[str, Any] = {"window_seconds": window_seconds, "lanes": {}}

    for lane in lanes:
        try:
            detected = await redis.zcount(f"omni:kpi:detected:{lane}", since, "+inf")
            resolved = await redis.zcount(f"omni:kpi:resolved:{lane}", since, "+inf")
        except Exception:
            detected, resolved = 0, 0
        trend["lanes"][lane] = {"detected": int(detected), "resolved": int(resolved)}

    return trend


@router.get("/summary")
async def kpi_summary(request: Request) -> JSONResponse:
    redis = _get_redis(request)
    data = await _fetch_kpi_summary(redis)
    return JSONResponse(content=data)


@router.get("/trend")
async def kpi_trend(
    request: Request,
    window: str = Query(default="24h", pattern="^(1h|6h|24h|7d)$"),
) -> JSONResponse:
    redis = _get_redis(request)
    window_seconds = _WINDOW_MAP.get(window, 86400)
    data = await _fetch_kpi_trend(redis, window_seconds)
    return JSONResponse(content=data)
