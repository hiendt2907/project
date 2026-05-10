"""ContextVar trace_id cho Gateway — mọi log trong cùng request async có cùng trace (filter inject)."""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token

_trace_var: ContextVar[str] = ContextVar("gateway_trace_id", default="")


def current_gateway_trace_id() -> str:
    return _trace_var.get() or ""


def push_gateway_trace_id(trace_id: str) -> Token:
    return _trace_var.set(trace_id)


def pop_gateway_trace_id(token: Token) -> None:
    _trace_var.reset(token)


class OmniGatewayTraceFilter(logging.Filter):
    """Gắn ``[trace_id=…]`` vào mọi log record khi ContextVar đang set (uvicorn/asyncio/aiohttp gồm)."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 — logging API
        tid = current_gateway_trace_id()
        if not tid or getattr(record, "_omni_gateway_trace_done", False):
            return True
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if tid in msg:
            record._omni_gateway_trace_done = True  # type: ignore[attr-defined]
            return True
        record.msg = f"[trace_id={tid}] {msg}"
        record.args = ()
        record._omni_gateway_trace_done = True  # type: ignore[attr-defined]
        return True


def install_gateway_trace_logging() -> None:
    """Gọi một lần lúc startup (lifespan): gắn filter lên root + uvicorn + asyncio + aiohttp."""
    filt = OmniGatewayTraceFilter()
    for name in (
        "",
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
        "asyncio",
        "aiohttp.client",
        "aiohttp.connector",
        "aiohttp.web",
    ):
        logging.getLogger(name).addFilter(filt)
    # Ensure gateway namespace emits INFO to stdout (uvicorn sets root to WARNING by default)
    gw_logger = logging.getLogger("gateway")
    if not gw_logger.handlers:
        _h = logging.StreamHandler()
        _h.setLevel(logging.INFO)
        gw_logger.setLevel(logging.INFO)
        gw_logger.addHandler(_h)
        gw_logger.propagate = False
