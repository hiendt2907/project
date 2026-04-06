"""Per-message trace_id: ContextVar + structured start_request / end_request logs."""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from typing import Any

logger = logging.getLogger(__name__)


class OmniWorkerTraceFilter(logging.Filter):
    """Prefix every log line with ``[traceid:…]`` from :func:`current_trace_id` (async-local)."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 — logging API
        if getattr(record, "_omni_worker_trace_done", False):
            return True
        tid = current_trace_id()
        mark = tid if tid != "unknown" else "-"
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "[traceid:" in msg:
            record._omni_worker_trace_done = True  # type: ignore[attr-defined]
            return True
        # Already includes the active trace (e.g. "[%s] event=start_request", trace_id, ...).
        if tid != "unknown" and tid in msg:
            record._omni_worker_trace_done = True  # type: ignore[attr-defined]
            return True
        record.msg = f"[traceid:{mark}] {msg}"
        record.args = ()
        record._omni_worker_trace_done = True  # type: ignore[attr-defined]
        return True


def install_worker_trace_logging(root: logging.Logger | None = None) -> None:
    """Attach :class:`OmniWorkerTraceFilter` to the root logger (call once from ``run_worker``)."""
    filt = OmniWorkerTraceFilter()
    (root or logging.getLogger()).addFilter(filt)

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


def log_start_request_ctx(*, phase: str, **fields: Any) -> None:
    """Giống :func:`log_start_request` nhưng lấy trace từ ContextVar (sau ``push_trace_id`` / decorator)."""
    log_start_request(current_trace_id(), phase=phase, **fields)


def log_end_request_ctx(
    *,
    phase: str,
    status: str,
    duration_ms: float,
    **fields: Any,
) -> None:
    """Giống :func:`log_end_request` nhưng lấy trace từ ContextVar."""
    log_end_request(current_trace_id(), phase=phase, status=status, duration_ms=duration_ms, **fields)


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
