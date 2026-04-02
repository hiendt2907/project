"""Per-message trace_id: ContextVar + structured start_request / end_request logs."""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from typing import Any

logger = logging.getLogger(__name__)

_trace_id_var: ContextVar[str] = ContextVar("inbound_trace_id", default="")


def current_trace_id() -> str:
    """Trace id for the active inbound request (or ``\"unknown\"``)."""
    v = _trace_id_var.get()
    return v if v else "unknown"


def push_trace_id(trace_id: str) -> Token:
    """Bind ``trace_id`` for this task; reset with :func:`pop_trace_id`."""
    return _trace_id_var.set(trace_id)


def pop_trace_id(token: Token) -> None:
    _trace_id_var.reset(token)


def _fmt_kv(**fields: Any) -> str:
    parts: list[str] = []
    for k in sorted(fields):
        v = fields[k]
        if v is None:
            continue
        parts.append(f"{k}={v!r}")
    return " ".join(parts)


def log_start_request(trace_id: str, *, phase: str, **fields: Any) -> None:
    """Structured log: ``event=start_request`` (grep-friendly)."""
    extra = _fmt_kv(**fields)
    if extra:
        logger.info("[%s] event=start_request phase=%s %s", trace_id, phase, extra)
    else:
        logger.info("[%s] event=start_request phase=%s", trace_id, phase)


def log_end_request(
    trace_id: str,
    *,
    phase: str,
    status: str,
    duration_ms: float,
    **fields: Any,
) -> None:
    """Structured log: ``event=end_request`` — always pair with :func:`log_start_request` same ``phase``."""
    extra = _fmt_kv(**fields)
    if extra:
        logger.info(
            "[%s] event=end_request phase=%s status=%s duration_ms=%.2f %s",
            trace_id,
            phase,
            status,
            duration_ms,
            extra,
        )
    else:
        logger.info(
            "[%s] event=end_request phase=%s status=%s duration_ms=%.2f",
            trace_id,
            phase,
            status,
            duration_ms,
        )
