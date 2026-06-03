"""Trace session route — read-only diagnosis session for T3 drill-down.

Reads the full multi-turn diagnosis session stored by diagnosis_loop at
`omni:diag:session:{trace_id}` (INV_DIAG_STORED). Returns turns, per-turn
LLM reasoning, requested commands, and command results.

SECURITY (metadata-only commitment): command stdout is NEVER returned in full.
Each result is reduced to a head+tail preview so the UI can show that a command
ran and roughly what it returned, without exfiltrating VM file/DB content.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

router = APIRouter(prefix="/trace", tags=["trace"])

_SESSION_KEY_PREFIX = "omni:diag:session:"
_PREVIEW_HEAD_LINES = 6
_PREVIEW_TAIL_LINES = 3
_PREVIEW_LINE_CAP = 160


def _get_redis(request: Request) -> Any:
    r = getattr(request.app.state, "redis", None)
    if r is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return r


def _preview_output(text: str) -> dict[str, Any]:
    """Reduce command stdout to a bounded head+tail preview (metadata-only)."""
    if not text:
        return {"head": [], "tail": [], "total_lines": 0, "truncated": False}
    lines = text.splitlines()
    total = len(lines)
    cap = lambda s: s[:_PREVIEW_LINE_CAP]  # noqa: E731
    if total <= _PREVIEW_HEAD_LINES + _PREVIEW_TAIL_LINES:
        return {"head": [cap(l) for l in lines], "tail": [], "total_lines": total, "truncated": False}
    head = [cap(l) for l in lines[:_PREVIEW_HEAD_LINES]]
    tail = [cap(l) for l in lines[-_PREVIEW_TAIL_LINES:]]
    return {"head": head, "tail": tail, "total_lines": total, "truncated": True}


def _sanitize_result(res: dict[str, Any]) -> dict[str, Any]:
    """Strip full stdout/stderr; keep metadata + bounded preview."""
    return {
        "cmd_id": res.get("cmd_id", ""),
        "command_str": res.get("command_str", ""),
        "purpose": res.get("purpose", ""),
        "rc": res.get("rc", 0),
        "status": res.get("status", "ok"),
        "blocked": bool(res.get("blocked", False)),
        "block_reason": res.get("block_reason", ""),
        "preview": _preview_output(res.get("stdout", "") or ""),
        "stderr_preview": (res.get("stderr", "") or "")[:300],
    }


def _sanitize_turn(turn: dict[str, Any]) -> dict[str, Any]:
    return {
        "turn": turn.get("turn", 0),
        "reasoning": turn.get("reasoning", ""),
        "hypothesis": turn.get("hypothesis", ""),
        "evidence_gaps": turn.get("evidence_gaps", []),
        "confidence": turn.get("confidence", 0.0),
        "commands_requested": turn.get("commands_requested", []),
        "command_results": [_sanitize_result(r) for r in turn.get("command_results", [])],
        "diagnosis_complete_claimed": bool(turn.get("diagnosis_complete_claimed", False)),
    }


@router.get("/{trace_id}/session")
async def trace_session(trace_id: str, request: Request) -> JSONResponse:
    """Return the sanitized multi-turn diagnosis session for a trace."""
    if not trace_id or len(trace_id) > 128:
        raise HTTPException(status_code=400, detail="invalid trace_id")
    redis = _get_redis(request)
    raw = await redis.get(f"{_SESSION_KEY_PREFIX}{trace_id}")
    if raw is None:
        return JSONResponse(
            {"trace_id": trace_id, "found": False, "source": "gateway"},
            status_code=404,
        )
    try:
        session = json.loads(raw)
    except (ValueError, TypeError) as exc:
        log.error("[trace] corrupt session trace=%s err=%s", trace_id, exc)
        raise HTTPException(status_code=500, detail="corrupt session") from exc

    final = session.get("final") or {}
    return JSONResponse({
        "found": True,
        "source": "gateway",
        "trace_id": session.get("trace_id", trace_id),
        "agent_id": session.get("agent_id", ""),
        "probe": session.get("probe", ""),
        "lane": session.get("lane", ""),
        "alert_hint": session.get("alert_hint", ""),
        "total_turns": session.get("total_turns", 0),
        "degraded": bool(session.get("degraded", False)),
        "degraded_reason": session.get("degraded_reason", ""),
        "completed_at": session.get("completed_at", 0),
        "turns": [_sanitize_turn(t) for t in session.get("turns", [])],
        "final": {
            "root_cause": final.get("root_cause", ""),
            "affected_components": final.get("affected_components", []),
            "blast_radius": final.get("blast_radius", ""),
            "impact_summary": final.get("impact_summary", ""),
            "remediation_steps": final.get("remediation_steps", []),
            "confidence": final.get("confidence", 0.0),
        },
    })
