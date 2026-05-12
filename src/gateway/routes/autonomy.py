"""Autonomy policy management endpoints — read/write fine-grained autonomy rules."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from pkg.autonomy.policy import AutonomyPolicyStore, PolicyRule

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/autonomy", tags=["autonomy"])

_store = AutonomyPolicyStore()


def _get_redis(request: Request) -> Any:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return redis


@router.get("/policy")
async def get_policy(request: Request) -> JSONResponse:
    """Return the current autonomy policy list as JSON (ordered; first match wins)."""
    redis = _get_redis(request)
    try:
        rules = await _store.get_policy(redis)
        return JSONResponse(content={"policy": [r.model_dump() for r in rules]})
    except Exception as exc:
        logger.error("autonomy.get_policy error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/policy/rule")
async def add_policy_rule(request: Request, rule: PolicyRule) -> JSONResponse:
    """Prepend a new rule to the policy list. The new rule takes highest priority."""
    redis = _get_redis(request)
    try:
        await _store.set_rule(redis, rule)
        return JSONResponse(content={"status": "ok", "rule": rule.model_dump()})
    except Exception as exc:
        logger.error("autonomy.add_policy_rule error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/policy/history")
async def get_policy_history(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
) -> JSONResponse:
    """Return the policy change history (most recent first)."""
    redis = _get_redis(request)
    try:
        history = await _store.get_history(redis, limit=limit)
        return JSONResponse(content={"history": history})
    except Exception as exc:
        logger.error("autonomy.get_policy_history error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/policy/reset")
async def reset_policy(request: Request) -> JSONResponse:
    """Reset policy to built-in defaults. Requires API key (enforced at router level)."""
    redis = _get_redis(request)
    try:
        await _store.reset_to_defaults(redis)
        rules = await _store.get_policy(redis)
        return JSONResponse(
            content={
                "status": "reset",
                "policy": [r.model_dump() for r in rules],
            }
        )
    except Exception as exc:
        logger.error("autonomy.reset_policy error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
