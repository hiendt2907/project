"""ToolBackend registry delegate."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from workers.tool_backend import RegistryToolBackend


@pytest.mark.asyncio
async def test_registry_tool_backend_invokes_registry() -> None:
    calls: list[tuple[str, dict]] = []

    async def tool_fn(ctx: object, args: dict) -> str:
        calls.append(("t", args))
        return "ok"

    reg = {"mytool": tool_fn}
    backend = RegistryToolBackend(registry=reg)
    ctx = MagicMock()
    out = await backend.invoke(ctx, "mytool", {"a": 1})
    assert out == "ok"
    assert calls == [("t", {"a": 1})]
