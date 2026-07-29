"""Trace session route — read-only diagnosis session for T3 drill-down.

Reads the full multi-turn diagnosis session stored by diagnosis_loop at
`omni:diag:session:{trace_id}` (INV_DIAG_STORED). Returns turns, per-turn
LLM reasoning, requested commands, and command results.

SECURITY (metadata-only commitment): command stdout is NEVER returned in full.
Each result is reduced to a head+tail preview so the UI can show that a command
ran and roughly what it returned, without exfiltrating VM file/DB content.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncGenerator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

# Shared module under src/pkg/ — packaged into the gateway image (see Dockerfile.gateway).
# Gateway must NOT import `workers`; pkg.observability is the dependency-light shared home.
from pkg.observability.pipeline_stages import PIPELINE_STAGES, mark_stage  # noqa: F401

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


_STAGES_KEY_PREFIX = "omni:trace:stages:"
_EVENTS_STREAM = "omni:trace:events"
_SSE_BLOCK_MS = 2000


@router.get("/stream")
async def trace_stream(request: Request) -> StreamingResponse:
    """SSE stream of pipeline stage events from the global omni:trace:events stream.

    Yields ``data: <json>\\n\\n`` per event. Starts from $ (new events only).
    Ends gracefully on Redis error or client disconnect.
    """
    redis = _get_redis(request)

    async def _generate() -> AsyncGenerator[str, None]:
        # Flush a comment + retry hint immediately so the proxy (Traefik) forwards
        # response headers and starts streaming instead of waiting for the first
        # real event. Without this, an idle stream sends no bytes and gets buffered.
        yield ": connected\nretry: 3000\n\n"
        last_id = "$"
        while True:
            if await request.is_disconnected():
                break
            try:
                results = await redis.xread(
                    {_EVENTS_STREAM: last_id}, block=_SSE_BLOCK_MS, count=50
                )
            except Exception as exc:
                log.warning("[trace/stream] redis error, ending stream: %s", exc)
                break
            if results:
                for _stream, messages in results:
                    for msg_id, fields in messages:
                        last_id = msg_id
                        try:
                            payload = json.dumps(
                                {
                                    "trace_id": fields.get("trace_id", ""),
                                    "stage": fields.get("stage", ""),
                                    "status": fields.get("status", ""),
                                    "lane": fields.get("lane", ""),
                                    "ts": fields.get("ts", ""),
                                },
                                ensure_ascii=False,
                            )
                            yield f"data: {payload}\n\n"
                        except Exception:
                            pass
            else:
                # Idle heartbeat keeps bytes flowing so proxies don't buffer/idle-timeout.
                yield ": ping\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(_generate(), media_type="text/event-stream")


_RECENT_SCAN = 500
_RECENT_LIMIT = 30


@router.get("/recent")
async def trace_recent(request: Request) -> JSONResponse:
    """Return the most recently active traces, derived from omni:trace:events.

    Reads the tail of the global event stream, de-duplicates by trace_id (newest
    first), and enriches each with lane + current stage/verdict from its stage hash.
    Replaces the UI mock list with real live data.
    """
    redis = _get_redis(request)
    try:
        # XREVRANGE newest→oldest; cap the scan window.
        entries = await redis.xrevrange(_EVENTS_STREAM, count=_RECENT_SCAN)
    except Exception as exc:
        log.warning("[trace/recent] redis error: %s", exc)
        return JSONResponse({"traces": [], "source": "error"})

    seen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for _msg_id, fields in entries or []:
        tid = fields.get("trace_id") or ""
        if not tid or tid in seen:
            continue
        seen[tid] = {
            "trace_id": tid,
            "lane": fields.get("lane", "") or "",
            "current_stage": fields.get("stage", "") or "",
            "verdict": "",
            "started_at": 0.0,
            "updated_at": float(fields.get("ts") or 0),
        }
        order.append(tid)
        if len(order) >= _RECENT_LIMIT:
            break

    # Enrich with meta + verdict from each stage hash (best-effort, bounded).
    traces = []
    for tid in order:
        rec = seen[tid]
        try:
            raw_all: dict[str, str] = await redis.hgetall(f"{_STAGES_KEY_PREFIX}{tid}")
        except Exception:
            raw_all = {}
        if raw_all.get("__meta__"):
            try:
                meta = json.loads(raw_all["__meta__"])
                rec["lane"] = meta.get("lane", "") or rec["lane"]
                rec["started_at"] = float(meta.get("started_at") or 0)
                rec["updated_at"] = float(meta.get("updated_at") or rec["updated_at"])
            except Exception:
                pass
        for st in ("SCHEMA", "DISPATCH"):
            if raw_all.get(st):
                try:
                    d = str(json.loads(raw_all[st]).get("detail") or "")
                    rec["verdict"] = d[len("verdict="):] if d.startswith("verdict=") else d
                    break
                except Exception:
                    pass
        traces.append(rec)

    return JSONResponse({"traces": traces, "source": "gateway"})


_LOGS_KEY_PREFIX = "omni:trace:logs:"


@router.get("/{trace_id}/logs")
async def trace_logs(trace_id: str, request: Request) -> JSONResponse:
    """Return the raw per-phase log stream for a trace (newest last).

    Each entry: ``{ts, phase, level, line}``. Empty list when nothing logged.
    """
    if not trace_id or len(trace_id) > 128:
        raise HTTPException(status_code=400, detail="invalid trace_id")
    redis = _get_redis(request)
    try:
        raw_list = await redis.lrange(f"{_LOGS_KEY_PREFIX}{trace_id}", 0, -1)
    except Exception as exc:
        log.warning("[trace/logs] redis error trace=%s err=%s", trace_id, exc)
        return JSONResponse({"trace_id": trace_id, "logs": [], "source": "error"})
    logs = []
    for raw in raw_list or []:
        try:
            logs.append(json.loads(raw))
        except (ValueError, TypeError):
            continue
    return JSONResponse({"trace_id": trace_id, "logs": logs, "source": "gateway"})


_BRAIN_KEY_PREFIX = "omni:brain:session:"


@router.get("/{trace_id}/brain")
async def trace_brain(trace_id: str, request: Request) -> JSONResponse:
    """Return the Redis second-brain multi-turn RAG session for a trace.

    Shows the iterative vector-store reasoning (turns, queries, hits, confidence)
    that fed the LLM. ``found: false`` (404) when the brain stored nothing.
    """
    if not trace_id or len(trace_id) > 128:
        raise HTTPException(status_code=400, detail="invalid trace_id")
    redis = _get_redis(request)
    raw = await redis.get(f"{_BRAIN_KEY_PREFIX}{trace_id}")
    if raw is None:
        return JSONResponse({"found": False, "trace_id": trace_id, "source": "gateway"}, status_code=404)
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        raise HTTPException(status_code=500, detail="corrupt brain session") from None
    return JSONResponse({"found": True, "source": "gateway", **doc})


_ADVISORY_KEY_PREFIX = "omni:trace:advisory:"


@router.get("/{trace_id}/advisory")
async def trace_advisory(trace_id: str, request: Request) -> JSONResponse:
    """Return the stored AnalystAdvisory for a trace (deep-check report).

    Populated by the remote-agent pipeline on single-pass advisories. Returns
    ``found: false`` (200) when no advisory was stored — e.g. the trace ran the
    multi-turn diagnosis loop instead (use /session), or was suppressed.
    """
    if not trace_id or len(trace_id) > 128:
        raise HTTPException(status_code=400, detail="invalid trace_id")
    redis = _get_redis(request)
    raw = await redis.get(f"{_ADVISORY_KEY_PREFIX}{trace_id}")
    if raw is None:
        return JSONResponse({"found": False, "trace_id": trace_id, "source": "gateway"}, status_code=404)
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        raise HTTPException(status_code=500, detail="corrupt advisory") from None
    return JSONResponse({"found": True, "source": "gateway", **doc})


@router.get("/{trace_id}/pipeline")
async def trace_pipeline(trace_id: str, request: Request) -> JSONResponse:
    """Return the pipeline stage progress for a trace.

    Stages not yet written are returned with status=pending, ts=0, elapsed_ms=0.
    Returns 404 if the trace hash does not exist in Redis.
    """
    if not trace_id or len(trace_id) > 128:
        raise HTTPException(status_code=400, detail="invalid trace_id")

    redis = _get_redis(request)
    key = f"{_STAGES_KEY_PREFIX}{trace_id}"

    try:
        raw_all: dict[str, str] = await redis.hgetall(key)
    except Exception as exc:
        log.error("[trace/pipeline] redis error trace=%s err=%s", trace_id, exc)
        raise HTTPException(status_code=503, detail="redis error") from exc

    if not raw_all:
        return JSONResponse(
            {
                "found": False,
                "trace_id": trace_id,
                "lane": "",
                "started_at": 0,
                "updated_at": 0,
                "verdict": "",
                "stages": _build_pending_stages(0),
            },
            status_code=404,
        )

    meta: dict[str, Any] = {}
    if "__meta__" in raw_all:
        try:
            meta = json.loads(raw_all["__meta__"])
        except Exception:
            meta = {}

    started_at: float = float(meta.get("started_at") or 0)

    # Parse each stage entry
    stage_data: dict[str, dict[str, Any]] = {}
    verdict = ""
    for stage in PIPELINE_STAGES:
        raw = raw_all.get(stage)
        if raw is None:
            continue
        try:
            entry: dict[str, Any] = json.loads(raw)
        except Exception:
            entry = {}
        stage_data[stage] = entry
        # Advisory verdict lives in SCHEMA detail as "verdict=<X>"; fall back to DISPATCH action.
        if not verdict and stage in ("SCHEMA", "DISPATCH") and entry.get("detail"):
            d = str(entry["detail"])
            verdict = d[len("verdict="):] if d.startswith("verdict=") else d

    stages_out = []
    for stage in PIPELINE_STAGES:
        if stage not in stage_data:
            stages_out.append(
                {"stage": stage, "status": "pending", "ts": 0, "detail": "", "elapsed_ms": 0}
            )
        else:
            entry = stage_data[stage]
            ts_val: float = float(entry.get("ts") or 0)
            elapsed_ms = int((ts_val - started_at) * 1000) if ts_val > 0 and started_at > 0 else 0
            stages_out.append(
                {
                    "stage": stage,
                    "status": entry.get("status", "pending"),
                    "ts": ts_val,
                    "detail": entry.get("detail", ""),
                    "elapsed_ms": max(0, elapsed_ms),
                }
            )

    return JSONResponse(
        {
            "found": True,
            "trace_id": trace_id,
            "lane": meta.get("lane", ""),
            "started_at": started_at,
            "updated_at": float(meta.get("updated_at") or 0),
            "verdict": verdict,
            "stages": stages_out,
        }
    )


@router.post("/purge")
async def trace_purge(request: Request) -> JSONResponse:
    """Clear all Active Trace dashboard state — self-service purge so an operator
    doesn't have to ask engineering to clear Redis by hand.

    Deletes the omni:trace:events stream (source for /recent) plus every
    omni:trace:stages:*, omni:trace:logs:*, omni:trace:advisory:* key. Evidence
    cluster dedup state (omni:evcluster:*) is left untouched on purpose — wiping
    the dashboard view must not erase incident memory used for repeat suppression.

    ADMIN ONLY: the wipe is cluster-wide across every tenant's keys, so a tenant key
    must not reach it. Until 2026-07-29 any authenticated caller could erase every
    tenant's diagnostic trace state.
    """
    from gateway.tenant_context import get_tenant_ctx, is_admin_ctx

    if not is_admin_ctx(get_tenant_ctx(request)):
        raise HTTPException(status_code=403, detail="Admin API key required")

    redis = _get_redis(request)
    deleted = 0
    try:
        deleted += await redis.delete(_EVENTS_STREAM)
        for prefix in (_STAGES_KEY_PREFIX, _LOGS_KEY_PREFIX, _ADVISORY_KEY_PREFIX):
            keys = [key async for key in redis.scan_iter(match=f"{prefix}*", count=500)]
            if keys:
                deleted += await redis.delete(*keys)
    except Exception as exc:
        log.error("[trace/purge] redis error: %s", exc)
        raise HTTPException(status_code=503, detail="redis error") from exc
    log.info("[trace/purge] cleared active traces, keys_deleted=%d", deleted)
    return JSONResponse({"purged": True, "keys_deleted": deleted})


def _build_pending_stages(started_at: float) -> list[dict[str, Any]]:
    return [
        {"stage": s, "status": "pending", "ts": 0, "detail": "", "elapsed_ms": 0}
        for s in PIPELINE_STAGES
    ]
