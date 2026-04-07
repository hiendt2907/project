"""Legacy approval hook: Redis manual workflow removed — funnel to Telegram escalation only."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Kept for tests that import prefix (legacy keys no longer written).
APPROVAL_KEY_PREFIX = "omni:approval:"


async def request_approval(
    ctx: Any,
    *,
    tool_name: str,
    args_summary: str,
    fp: str,
) -> bool:
    """Deny by default; notify admin via ``emit_telegram_escalation`` (single red-button contract)."""
    from workers.telegram_escalation import emit_telegram_escalation

    trace = str(getattr(ctx, "inbound_trace_id", None) or "approval-request")
    body = f"tool={tool_name}\nfp={fp}\nargs={args_summary[:1200]}"
    await emit_telegram_escalation(ctx, trace, body, reason="LEGACY_APPROVAL_REQUEST")
    return False


async def approval_status(ctx: Any, token: str) -> str | None:
    """Deprecated: Redis approval tokens no longer used. Returns None."""
    _ = (ctx, token)
    return None
