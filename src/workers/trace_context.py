"""Đồng bộ ContextVar trace cho Analyst — log không cần nhồi trace_id thủ công (filter request_trace)."""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from workers.request_trace import pop_trace_id, push_trace_id

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


@asynccontextmanager
async def inbound_trace_scope(trace_id: str) -> AsyncIterator[None]:
    """Gắn ``trace_id`` cho toàn bộ khối async (push/pop ContextVar)."""
    tok = push_trace_id(trace_id)
    try:
        yield
    finally:
        pop_trace_id(tok)


def trace_context(_fn: F | None = None, *, trace_kw: str = "trace_id") -> Any:
    """
    Decorator async: đọc ``trace_id`` từ kwarg (mặc định ``trace_id``) hoặc tham số tên ``trace``.
    Dùng sau khi đã parse payload (ví dụ ``reason_diagnostic_evidence_only`` nhận ``trace``).
    """

    def _decorator(fn: F) -> F:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError("trace_context chỉ hỗ trợ async function")

        @functools.wraps(fn)
        async def _wrapper(*args: Any, **kwargs: Any) -> Any:
            tid: str | None = kwargs.get(trace_kw)
            if tid is None:
                tid = kwargs.get("trace")
            if tid is None and len(args) >= 3:
                # (ctx, payload, trace) pattern
                tid = str(args[2])
            if not tid:
                logger.warning("trace_context: missing trace_id, using unknown")
                tid = "unknown"
            async with inbound_trace_scope(str(tid)):
                return await fn(*args, **kwargs)

        return _wrapper  # type: ignore[return-value]

    if _fn is not None:
        return _decorator(_fn)
    return _decorator
