"""Hooks for persisting verified outcomes into experience / RAG (governed paths)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def on_verified_resolve_hook(
    ctx: Any,
    *,
    trace_id: str,
    tool_name: str | None = None,
    summary: str = "",
) -> None:
    """Placeholder integration point: call archivist / action_experience upserts when wired.

    Keeps CRAT and ``omni_experience_requires_sdk_verify`` gating on concrete write paths;
    this hook logs and defers to existing save sites (proactive learning, routing_experience).
    """
    ws = getattr(ctx, "settings", None)
    if ws is None:
        return
    if not bool(getattr(ws, "action_experience_enabled", True)):
        return
    logger.info(
        "event=trace_orchestrator_verified_resolve trace=%s tool=%s summary_len=%s",
        trace_id,
        tool_name,
        len(summary or ""),
    )
