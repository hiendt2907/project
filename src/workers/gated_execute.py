"""Gated allowlisted cluster action — lazy-import execution.promotion để tránh vòng import."""

from __future__ import annotations

from typing import Any


async def tool_gated_allowlisted_execute(ctx: Any, args: dict[str, Any]) -> str:
    from execution.promotion import run_gated_allowlisted_execute

    return await run_gated_allowlisted_execute(ctx, args)
