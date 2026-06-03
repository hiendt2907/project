"""Agents route — read worker heartbeats (internal) + remote agent registry."""
from __future__ import annotations

import json
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from gateway.tenant_context import get_tenant_ctx, is_admin_ctx, resolve_scope

_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
_TENANT_ID_PATTERN = r"^[a-zA-Z0-9_-]+$"

router = APIRouter(prefix="/agents", tags=["agents"])

_HB_PREFIX = "omni:agent:heartbeat:"
_REMOTE_PREFIX = "omni:remote_agent:registry:"
_EPS_PREFIX = "omni:remote_agent:eps:"
_METRICS_PREFIX = "omni:remote_agent:metrics:"
_LOGS_PREFIX = "omni:remote_agent:logs:"
_STALE_SEC = 90
_REMOTE_STALE_SEC = 120
_EPS_WINDOW_MS = 60_000


def _get_redis(request: Request) -> Any:
    r = getattr(request.app.state, "redis", None)
    if r is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return r


@router.get("")
async def list_agents(
    request: Request,
    tenant_id: str | None = Query(default=None, max_length=64, pattern=_TENANT_ID_PATTERN),
) -> JSONResponse:
    """Return health of internal worker roles + connected remote agents."""
    redis = _get_redis(request)
    ctx = get_tenant_ctx(request)
    scope = resolve_scope(ctx, tenant_id)
    now = int(time.time())

    try:
        if scope:
            hb_keys = await redis.keys(f"{_HB_PREFIX}{scope}:*")
        else:
            hb_keys = await redis.keys(f"{_HB_PREFIX}*")
        remote_keys = await redis.keys(f"{_REMOTE_PREFIX}*")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis error: {e}") from e

    workers: list[dict] = []
    for key in sorted(hb_keys):
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
        hb["type"] = hb.get("type", "worker")
        if hb["stale"]:
            hb["status"] = "stale"
        workers.append(hb)

    remote_agents: list[dict] = []
    for key in sorted(remote_keys):
        raw = await redis.get(key)
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        if scope and rec.get("tenant_id") != scope:
            continue
        age = now - int(rec.get("last_seen", 0))
        rec["age_seconds"] = age
        rec["online"] = age <= _REMOTE_STALE_SEC
        rec["status"] = "online" if rec["online"] else "offline"
        remote_agents.append(rec)

    all_agents = workers + remote_agents
    overall = "ok"
    for a in all_agents:
        if a.get("status") == "unhealthy":
            overall = "unhealthy"
            break
        if a.get("status") in ("degraded", "stale", "offline"):
            overall = "degraded"

    return JSONResponse(content={
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "overall": overall,
        "workers": workers,
        "remote_agents": remote_agents,
        "count": len(all_agents),
    })


@router.get("/remote")
async def list_remote_agents(request: Request) -> JSONResponse:
    """List only remote agents (external Linux servers)."""
    redis = _get_redis(request)
    now = int(time.time())

    try:
        keys = await redis.keys(f"{_REMOTE_PREFIX}*")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis error: {e}") from e

    now_ms = int(time.time() * 1000)
    agents: list[dict] = []
    for key in sorted(keys):
        raw = await redis.get(key)
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        age = now - int(rec.get("last_seen", 0))
        rec["age_seconds"] = age
        rec["online"] = age <= _REMOTE_STALE_SEC
        rec["status"] = "online" if rec["online"] else "offline"

        agent_id = rec.get("agent_id", "")
        try:
            raw_metrics = await redis.get(f"{_METRICS_PREFIX}{agent_id}")
            rec["metrics"] = json.loads(raw_metrics) if raw_metrics else None
        except Exception:
            rec["metrics"] = None

        try:
            eps_count = await redis.zcount(
                f"{_EPS_PREFIX}{agent_id}", now_ms - _EPS_WINDOW_MS, "+inf"
            )
            rec["eps"] = round(int(eps_count) / 60.0, 4)
        except Exception:
            rec["eps"] = 0.0

        agents.append(rec)

    return JSONResponse(content={
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(agents),
        "online": sum(1 for a in agents if a["online"]),
        "agents": agents,
    })


@router.get("/remote/eps")
async def remote_agents_eps(request: Request) -> JSONResponse:
    """EPS per remote agent — events received in the last 60 s."""
    redis = _get_redis(request)
    now_ms = int(time.time() * 1000)

    try:
        keys = await redis.keys(f"{_EPS_PREFIX}*")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis error: {e}") from e

    eps_map: dict[str, float] = {}
    total_events = 0
    for key in keys:
        agent_id = key.replace(_EPS_PREFIX, "") if isinstance(key, str) else key.decode().replace(_EPS_PREFIX, "")
        try:
            count = await redis.zcount(key, now_ms - _EPS_WINDOW_MS, "+inf")
            eps = round(int(count) / 60.0, 4)
            eps_map[agent_id] = eps
            total_events += int(count)
        except Exception:
            eps_map[agent_id] = 0.0

    return JSONResponse(content={
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window_seconds": 60,
        "total_eps": round(total_events / 60.0, 4),
        "agents": eps_map,
    })


@router.get("/remote/{agent_id}/logs")
async def remote_agent_logs(agent_id: str, request: Request, n: int = 50) -> JSONResponse:
    """Return last N log evidence entries for a remote agent."""
    if not _AGENT_ID_RE.fullmatch(agent_id):
        raise HTTPException(status_code=422, detail="Invalid agent_id")
    redis = _get_redis(request)
    log_key = f"{_LOGS_PREFIX}{agent_id}"
    metrics_key = f"{_METRICS_PREFIX}{agent_id}"

    try:
        raw_logs = await redis.lrange(log_key, 0, min(n, 100) - 1)
        raw_metrics = await redis.get(metrics_key)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis error: {e}") from e

    logs: list[dict] = []
    for raw in raw_logs:
        try:
            logs.append(json.loads(raw))
        except Exception:
            pass

    metrics = None
    if raw_metrics:
        try:
            metrics = json.loads(raw_metrics)
        except Exception:
            pass

    return JSONResponse(content={
        "agent_id": agent_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "logs": logs,
        "metrics": metrics,
    })


@router.delete("/remote/{agent_id}")
async def deregister_remote_agent(agent_id: str, request: Request) -> JSONResponse:
    """Force-deregister a remote agent — removes all Redis keys for this agent."""
    if not _AGENT_ID_RE.fullmatch(agent_id):
        raise HTTPException(status_code=422, detail="Invalid agent_id")
    redis = _get_redis(request)
    ctx = get_tenant_ctx(request)

    # Write guard: non-admin tenants can only delete their own agents
    if ctx is not None and not ctx.is_admin:
        raw = await redis.get(f"{_REMOTE_PREFIX}{agent_id}")
        if raw:
            try:
                rec = json.loads(raw)
                if rec.get("tenant_id") != ctx.tenant_id:
                    raise HTTPException(status_code=403, detail="tenant mismatch")
            except HTTPException:
                raise
            except Exception:
                pass

    keys_to_delete = [
        f"{_REMOTE_PREFIX}{agent_id}",
        f"{_METRICS_PREFIX}{agent_id}",
        f"{_EPS_PREFIX}{agent_id}",
        f"{_LOGS_PREFIX}{agent_id}",
    ]
    deleted = 0
    for k in keys_to_delete:
        try:
            deleted += await redis.delete(k)
        except Exception:
            pass
    return JSONResponse(content={"deregistered": agent_id, "keys_deleted": deleted})
