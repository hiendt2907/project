"""Tests for workers.trace_context (no unittest.mock)."""

from __future__ import annotations

import pytest

from workers.request_trace import current_trace_id
from workers.trace_context import inbound_trace_scope, trace_context


@pytest.mark.asyncio
async def test_inbound_trace_scope_sets_context() -> None:
    before = current_trace_id()
    async with inbound_trace_scope("trace-scope-aa"):
        assert current_trace_id() == "trace-scope-aa"
    after = current_trace_id()
    assert after == before


@pytest.mark.asyncio
async def test_trace_context_decorator_kwarg() -> None:
    @trace_context
    async def f(*, trace_id: str) -> str:
        return current_trace_id()

    out = await f(trace_id="tid-123")
    assert out == "tid-123"


@pytest.mark.asyncio
async def test_trace_context_decorator_positional_trace() -> None:
    @trace_context
    async def g(_a: object, _b: object, trace: str) -> str:
        return current_trace_id()

    out = await g(None, None, "from-pos")
    assert out == "from-pos"


@pytest.mark.asyncio
async def test_trace_context_missing_falls_back_to_unknown() -> None:
    @trace_context
    async def h() -> str:
        return current_trace_id()

    out = await h()
    assert out == "unknown"


def test_trace_context_rejects_sync_function() -> None:
    with pytest.raises(TypeError, match="async"):

        @trace_context
        def not_async() -> None:  # type: ignore[misc]
            pass
