"""Single entry: human escalation via Telegram (red-button contract)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_MAX_CTX = 3500


def format_operator_action_card(
    known_facts: dict[str, str],
    missing_facts: list[str],
    suggested_steps: list[str],
) -> str:
    """Return a structured KNOWN/MISSING/NEXT body for Telegram escalation messages."""
    lines: list[str] = ["KNOWN:"]
    for k, v in (known_facts or {}).items():
        lines.append(f"  {k}: {v}")
    if not known_facts:
        lines.append("  (no identity context extracted)")

    lines += ["", "MISSING:"]
    for m in (missing_facts or []):
        lines.append(f"  {m}")
    if not missing_facts:
        lines.append("  (nothing identified as missing)")

    lines += ["", "NEXT:"]
    for s in (suggested_steps or []):
        lines.append(f"  {s}")
    if not suggested_steps:
        lines.append("  escalate to on-call — insufficient context for automated action")

    return "\n".join(lines)


async def emit_telegram_escalation(
    ctx: Any,
    trace_id: str,
    context: str,
    *,
    reason: str = "ESCALATE",
) -> None:
    """
    One funnel for: max verify rounds, max mutate attempts, replan empty, ESCALATE verdict, manual escalation.
    No Redis approval workflow — notify admin chat only.
    """
    tid = str(trace_id or "").strip() or "unknown"
    ws = getattr(ctx, "settings", None)
    tg = getattr(ctx, "telegram", None)
    cid = getattr(ws, "telegram_admin_chat_id", None) if ws else None
    if tg is None or cid is None:
        logger.warning(
            "event=telegram_escalation_skip trace=%s reason=%s (no telegram or admin chat)",
            tid,
            reason,
        )
        return
    body = (context or "").strip()
    if len(body) > _MAX_CTX:
        body = body[: _MAX_CTX - 20] + "\n…(truncated)"
    msg = (
        f"[RED_ESCALATION] trace={tid}\n"
        f"reason={reason}\n"
        f"{body}"
    )
    try:
        await tg.send_message(int(cid), msg[:4000])
        logger.info("event=telegram_escalation_sent trace=%s reason=%s", tid, reason)
    except Exception as e:
        logger.warning("emit_telegram_escalation failed: %s", e)
