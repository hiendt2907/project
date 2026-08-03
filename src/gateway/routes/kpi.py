"""KPI summary and trend endpoints — read-only, no pipeline coupling."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from gateway.tenant_context import get_tenant_ctx, resolve_scope
from pkg.domain.taxonomy import CANONICAL_DOMAINS, UNKNOWN

router = APIRouter(prefix="/kpi", tags=["kpi"])

_WINDOW_MAP = {
    "1h": 3600,
    "6h": 21600,
    "24h": 86400,
    "7d": 604800,
}

_WINDOW_SECONDS = 86400

_TENANT_ID_PATTERN = r"^[a-zA-Z0-9_-]+$"


def _get_redis(request: Request) -> Any:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return redis


async def _fetch_kpi_summary(redis: Any, tenant_id: str | None) -> dict:
    now = time.time()
    since = now - _WINDOW_SECONDS

    if tenant_id is not None:
        accepted = int(await redis.zcount(f"omni:kpi:z:{tenant_id}:accepted", since, "+inf") or 0)
        rejected = int(await redis.zcount(f"omni:kpi:z:{tenant_id}:rejected", since, "+inf") or 0)
        false_pos = int(await redis.zcount(f"omni:kpi:z:{tenant_id}:false_positive", since, "+inf") or 0)
    else:
        accepted = 0
        async for key in redis.scan_iter("omni:kpi:z:*:accepted", count=100):
            accepted += int(await redis.zcount(key, since, "+inf") or 0)
        rejected = 0
        async for key in redis.scan_iter("omni:kpi:z:*:rejected", count=100):
            rejected += int(await redis.zcount(key, since, "+inf") or 0)
        false_pos = 0
        async for key in redis.scan_iter("omni:kpi:z:*:false_positive", count=100):
            false_pos += int(await redis.zcount(key, since, "+inf") or 0)

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


async def _fetch_kpi_trend(redis: Any, tenant_id: str | None, window_seconds: int) -> dict:
    now = time.time()
    since = now - window_seconds
    # Nhóm theo 9 domain canonical thay vì 4 lane. `unknown` được giữ trong danh sách
    # vì dữ liệu lịch sử `SYS_HARD_FAIL` cố ý đổ về đó (nó gánh 4 domain, suy ra một
    # domain cụ thể là đoán bừa) — bỏ `unknown` khỏi báo cáo là làm hụt số thật.
    domains = [*CANONICAL_DOMAINS, UNKNOWN]
    trend: dict[str, Any] = {"window_seconds": window_seconds, "domains": {}}

    for domain in domains:
        try:
            if tenant_id is not None:
                detected = int(await redis.zcount(f"omni:kpi:detected:{tenant_id}:{domain}", since, "+inf") or 0)
                resolved = int(await redis.zcount(f"omni:kpi:resolved:{tenant_id}:{domain}", since, "+inf") or 0)
            else:
                detected = 0
                async for key in redis.scan_iter(f"omni:kpi:detected:*:{domain}", count=100):
                    detected += int(await redis.zcount(key, since, "+inf") or 0)
                resolved = 0
                async for key in redis.scan_iter(f"omni:kpi:resolved:*:{domain}", count=100):
                    resolved += int(await redis.zcount(key, since, "+inf") or 0)
        except Exception:
            detected, resolved = 0, 0
        trend["domains"][domain] = {"detected": int(detected), "resolved": int(resolved)}

    # `lanes`: alias tương thích ngược cho UI/E2E chưa đổi sang `domains`. Cùng một
    # object, không phải bản sao — giữ hai khoá đồng bộ mà không nhân đôi vòng quét.
    trend["lanes"] = trend["domains"]
    return trend


@router.get("/summary")
async def kpi_summary(
    request: Request,
    tenant_id: str | None = Query(default=None, max_length=64, pattern=_TENANT_ID_PATTERN),
) -> JSONResponse:
    redis = _get_redis(request)
    ctx = get_tenant_ctx(request)
    scope = resolve_scope(ctx, tenant_id)
    data = await _fetch_kpi_summary(redis, scope)
    return JSONResponse(content=data)


@router.get("/trend")
async def kpi_trend(
    request: Request,
    window: str = Query(default="24h", pattern="^(1h|6h|24h|7d)$"),
    tenant_id: str | None = Query(default=None, max_length=64, pattern=_TENANT_ID_PATTERN),
) -> JSONResponse:
    redis = _get_redis(request)
    ctx = get_tenant_ctx(request)
    scope = resolve_scope(ctx, tenant_id)
    window_seconds = _WINDOW_MAP.get(window, 86400)
    data = await _fetch_kpi_trend(redis, scope, window_seconds)
    return JSONResponse(content=data)


@router.get("/clusters")
async def kpi_clusters(request: Request) -> JSONResponse:
    """S3.1: Active incident clusters with member counts."""
    redis = _get_redis(request)
    clusters: list[dict[str, Any]] = []
    try:
        async for key in redis.scan_iter(match="omni:cluster:*", count=200):
            key_str = key.decode() if isinstance(key, bytes) else str(key)
            if ":meta:" in key_str:
                continue
            try:
                raw = await redis.hgetall(key)
                if not raw:
                    continue
                def _s(k: str) -> str:
                    v = raw.get(k.encode()) or raw.get(k) or b""
                    return v.decode() if isinstance(v, bytes) else str(v)
                clusters.append({
                    "cluster_id": _s("cluster_id"),
                    "namespace": _s("namespace"),
                    "member_count": int(_s("member_count") or 0),
                    "created_at": float(_s("created_at") or 0),
                })
            except Exception:
                pass
    except Exception as e:
        return JSONResponse(content={"error": str(e), "clusters": []}, status_code=503)

    clusters.sort(key=lambda c: c["member_count"], reverse=True)
    return JSONResponse(content={"total": len(clusters), "clusters": clusters})


@router.get("/prompt-ab")
async def kpi_prompt_ab(request: Request) -> JSONResponse:
    """S3.3: Prompt A/B test variant statistics and winner."""
    redis = _get_redis(request)
    result: dict[str, Any] = {"variants": {}}
    try:
        for variant in ("A", "B"):
            raw = await redis.hgetall(f"omni:prompt:ab:{variant}")
            if not raw:
                result["variants"][variant] = {}
                continue
            def _f(k: str) -> float:
                v = raw.get(k.encode()) or raw.get(k) or 0
                return float(v)
            total = _f("total")
            result["variants"][variant] = {
                "total": int(total),
                "json_ok": int(_f("json_ok")),
                "json_ok_rate": round(_f("json_ok") / total, 4) if total else None,
                "avg_steps": round(_f("steps_sum") / total, 2) if total else None,
                "success": int(_f("success")),
                "success_rate": round(_f("success") / total, 4) if total else None,
            }
        winner_raw = await redis.get("omni:prompt:ab:winner")
        winner_at_raw = await redis.get("omni:prompt:ab:winner_at")
        result["winner"] = (winner_raw.decode() if isinstance(winner_raw, bytes) else winner_raw) if winner_raw else None
        result["winner_at"] = (winner_at_raw.decode() if isinstance(winner_at_raw, bytes) else winner_at_raw) if winner_at_raw else None
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=503)
    return JSONResponse(content=result)
