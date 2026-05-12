"""Playbook routes — read-only list + HITL approve/reject via FinGuard API."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

log = logging.getLogger(__name__)

router = APIRouter(prefix="/playbooks", tags=["playbooks"])

_HITL_API_BASE = os.getenv("HITL_API_BASE_URL", "http://hitl-api.finguard-customer.svc.cluster.local:8080").rstrip("/")
_HITL_API_TOKEN = os.getenv("HITL_API_TOKEN", "")
_HITL_TIMEOUT = float(os.getenv("HITL_API_TIMEOUT_SEC", "10"))

_PB_KEY_PREFIX = "pb:"
_STATE_KEY = "omni:playbook:state:{trace}:{pb_id}"


def _get_redis(request: Request) -> Any:
    r = getattr(request.app.state, "redis", None)
    if r is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return r


def _hitl_headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if _HITL_API_TOKEN:
        h["Authorization"] = f"Bearer {_HITL_API_TOKEN}"
    return h


class DecisionBody(BaseModel):
    trace_id: str
    reason: str = ""


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


@router.post("/{incident_id}/approve")
async def approve_hitl(incident_id: str, body: DecisionBody) -> JSONResponse:
    """Forward HITL approval decision to FinGuard API."""
    return await _post_decision(incident_id, "approved", body.reason)


@router.post("/{incident_id}/reject")
async def reject_hitl(incident_id: str, body: DecisionBody) -> JSONResponse:
    """Forward HITL rejection decision to FinGuard API."""
    return await _post_decision(incident_id, "rejected", body.reason)


async def _post_decision(incident_id: str, decision: str, reason: str) -> JSONResponse:
    url = f"{_HITL_API_BASE}/v1/hitl/decisions"
    payload = {"incident_id": incident_id, "decision": decision}
    if reason:
        payload["reason"] = reason

    def _do_request() -> tuple[int, str]:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers=_hitl_headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=_HITL_TIMEOUT) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    try:
        status_code, body = await asyncio.get_event_loop().run_in_executor(None, _do_request)
    except TimeoutError as e:
        log.warning("event=hitl_timeout incident=%s err=%s", incident_id, e)
        raise HTTPException(status_code=504, detail="FinGuard HITL API timeout") from e
    except Exception as e:
        log.error("event=hitl_call_error incident=%s err=%s", incident_id, e)
        raise HTTPException(status_code=502, detail=f"FinGuard HITL API unreachable: {e}") from e

    if status_code >= 400:
        log.warning("event=hitl_decision_rejected status=%d body=%s", status_code, body[:200])
        raise HTTPException(status_code=status_code, detail=body[:500])

    log.info("event=hitl_decision_posted incident=%s decision=%s", incident_id, decision)
    return JSONResponse(content={"ok": True, "incident_id": incident_id, "decision": decision})
