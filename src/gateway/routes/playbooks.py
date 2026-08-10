"""Playbook routes — read-only list. HITL approve/reject sống ở
gateway/routes/autonomy.py::decide_hitl (POST /autonomy/hitl/{pending_id}/decide) —
đường nội bộ dùng omni_admin.hitl_decision + CRAT, không còn forward ra API ngoài
(FinGuard đã gộp vào Smart SIEM nội bộ, xem plans/finguard-to-smart-siem-merge-2026-08-04.md).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

router = APIRouter(prefix="/playbooks", tags=["playbooks"])

_PB_KEY_PREFIX = "pb:"
_STATE_KEY = "omni:playbook:state:{trace}:{pb_id}"


def _get_redis(request: Request) -> Any:
    r = getattr(request.app.state, "redis", None)
    if r is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return r


@router.get("")
async def list_playbooks(request: Request) -> JSONResponse:
    """Return all playbooks stored in Redis JSON (`pb:*` keys)."""
    redis = _get_redis(request)
    try:
        keys = await redis.keys(f"{_PB_KEY_PREFIX}*")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis scan failed: {e}") from e

    results: list[dict] = []
    for key in sorted(keys):
        try:
            raw = await redis.execute_command("JSON.GET", key)
            if raw:
                pb = json.loads(raw) if isinstance(raw, str) else raw
                results.append(pb)
        except Exception as e:
            log.warning("event=playbook_read_error key=%s err=%s", key, e)

    return JSONResponse(content={"playbooks": results, "total": len(results)})


@router.get("/{playbook_id}")
async def get_playbook(playbook_id: str, request: Request) -> JSONResponse:
    redis = _get_redis(request)
    key = f"{_PB_KEY_PREFIX}{playbook_id}"
    try:
        raw = await redis.execute_command("JSON.GET", key)
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if not raw:
        raise HTTPException(status_code=404, detail=f"Playbook '{playbook_id}' not found")
    return JSONResponse(content=json.loads(raw) if isinstance(raw, str) else raw)


@router.get("/{playbook_id}/state")
async def get_playbook_state(playbook_id: str, trace_id: str, request: Request) -> JSONResponse:
    """Return StepStateMachine state for a specific trace+playbook."""
    redis = _get_redis(request)
    key = _STATE_KEY.format(trace=trace_id, pb_id=playbook_id)
    raw = await redis.get(key)
    if not raw:
        raise HTTPException(status_code=404, detail="No active state for this trace+playbook")
    return JSONResponse(content=json.loads(raw))


