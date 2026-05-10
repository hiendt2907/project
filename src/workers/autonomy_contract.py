"""Shared autonomous transition and terminal-tombstone contract."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

TRANSITION_INGESTED = "INGESTED"
TRANSITION_CONTEXT_READY = "CONTEXT_READY"
TRANSITION_DIAGNOSED = "DIAGNOSED"
TRANSITION_PLAN_EMITTED = "PLAN_EMITTED"
TRANSITION_EXECUTED = "EXECUTED"
TRANSITION_VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
TRANSITION_STATE_MACHINE_VERIFIED = "STATE_MACHINE_VERIFIED"
TRANSITION_REQUIRES_HUMAN = "REQUIRES_HUMAN"
TRANSITION_POST_VERIFY_STATE_OK = "POST_VERIFY_STATE_OK"
TRANSITION_POST_VERIFY_STATE_FAIL = "POST_VERIFY_STATE_FAIL"
TRANSITION_OS_RUNBOOK_EMITTED = "OS_RUNBOOK_EMITTED"
TRANSITION_DRY_RUN_PASSED = "DRY_RUN_PASSED"
TRANSITION_DRY_RUN_FAILED = "DRY_RUN_FAILED"
TRANSITION_COMMAND_FEEDBACK_INGESTED = "COMMAND_FEEDBACK_INGESTED"
TRANSITION_RE_EVALUATED = "RE_EVALUATED"


async def _next_seq(redis_cli: Any, trace_id: str) -> int:
    if redis_cli is None or not trace_id:
        return 0
    try:
        seq = int(await redis_cli.incr(f"omni:trace:transition_seq:{trace_id}"))
        await redis_cli.expire(f"omni:trace:transition_seq:{trace_id}", 7200)
        return seq
    except Exception:
        return 0


async def emit_transition(
    ctx: Any,
    *,
    trace_id: str,
    transition: str,
    status: str = "ok",
    component: str = "",
    detail: str = "",
    meta: dict[str, Any] | None = None,
) -> None:
    """Emit ordered transition evidence for one autonomous trace."""
    tid = str(trace_id or "").strip()
    if not tid:
        return
    seq = await _next_seq(getattr(ctx, "redis", None), tid)
    payload: dict[str, Any] = {
        "kind": "autonomy_transition",
        "trace_id": tid,
        "transition": transition,
        "status": status,
        "component": component or "unknown",
        "detail": (detail or "")[:2000],
        "sequence": seq,
        "ts": str(int(time.time())),
    }
    if meta:
        payload["meta"] = meta
    k = getattr(ctx, "kafka", None)
    ws = getattr(ctx, "settings", None)
    if k is not None and ws is not None:
        try:
            await k.send_dict(
                ws.kafka_topic_audit_agent,
                {"trace_id": tid, "data": json.dumps(payload, ensure_ascii=False)},
            )
        except Exception as e:
            logger.debug("emit_transition kafka skip: %s", e)
    try:
        d = (detail or "").strip()
        dlog = f" detail={d[:240]}" if d else ""
        logger.info(
            "[%s] event=autonomy_transition transition=%s status=%s component=%s seq=%s%s",
            tid,
            transition,
            status,
            component or "unknown",
            seq,
            dlog,
        )
    except Exception:
        pass


async def emit_terminal_tombstone(
    ctx: Any,
    *,
    trace_id: str,
    reason_code: str,
    component: str,
    detail: str = "",
    meta: dict[str, Any] | None = None,
) -> None:
    """Fail-closed terminal event for non-recoverable branches."""
    tid = str(trace_id or "").strip()
    if not tid:
        return
    tomb: dict[str, Any] = {
        "kind": "autonomy_tombstone",
        "trace_id": tid,
        "reason_code": reason_code,
        "component": component,
        "detail": (detail or "")[:2000],
        "ts": str(int(time.time())),
    }
    if meta:
        tomb["meta"] = meta
    await emit_transition(
        ctx,
        trace_id=tid,
        transition=TRANSITION_REQUIRES_HUMAN,
        status="error",
        component=component,
        detail=f"{reason_code}: {(detail or '')[:500]}",
        meta=meta or {},
    )
    k = getattr(ctx, "kafka", None)
    ws = getattr(ctx, "settings", None)
    if k is not None and ws is not None:
        try:
            await k.send_dict(
                ws.kafka_topic_dlq,
                {
                    "trace_id": tid,
                    "component": component,
                    "reason": reason_code,
                    "data": json.dumps(tomb, ensure_ascii=False),
                },
            )
        except Exception as e:
            logger.debug("emit_terminal_tombstone dlq skip: %s", e)
    try:
        await getattr(ctx, "redis").setex(
            f"omni:autonomous:terminal:{tid}",
            7200,
            json.dumps(tomb, ensure_ascii=False),
        )
    except Exception:
        pass
