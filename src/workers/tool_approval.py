"""P1 Human-in-the-loop: pending approval token trong Redis + Telegram admin."""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any

logger = logging.getLogger(__name__)

APPROVAL_KEY_PREFIX = "omni:approval:"


def _approval_ttl_sec(ctx: Any) -> int:
    ws = getattr(ctx, "settings", None)
    return int(getattr(ws, "approval_request_ttl_sec", 600) or 600)


async def request_approval(
    ctx: Any,
    *,
    tool_name: str,
    args_summary: str,
    fp: str,
) -> bool:
    """Ghi pending token Redis + (tuỳ chọn) Telegram.

    Trả về True chỉ khi key đã ở trạng thái approved (admin hoặc pipeline cập nhật).
    Mặc định pending → False (deny by default; polling ở tick sau qua approval_status).
    """
    redis = getattr(ctx, "redis", None)
    if redis is None:
        return False
    token = secrets.token_hex(12)
    key = f"{APPROVAL_KEY_PREFIX}{token}"
    payload = {
        "status": "pending",
        "tool": tool_name,
        "args_summary": (args_summary or "")[:1200],
        "fp": fp,
        "token": token,
    }
    raw = json.dumps(payload, ensure_ascii=False)
    ttl = _approval_ttl_sec(ctx)
    try:
        await redis.setex(key, ttl, raw)
    except Exception as e:
        logger.warning("request_approval setex failed: %s", e)
        return False

    ws = getattr(ctx, "settings", None)
    tg = getattr(ctx, "telegram", None)
    cid = getattr(ws, "telegram_admin_chat_id", None) if ws else None
    if tg is not None and cid is not None:
        try:
            msg = (
                f"[APPROVAL_PENDING] tool={tool_name} fp={fp}\n"
                f"token={token}\n"
                f"Approve: set Redis {key} JSON field status to approved (same TTL)."
            )
            await tg.send_message(int(cid), msg[:3900])
        except Exception as e:
            logger.warning("request_approval telegram: %s", e)

    try:
        cur = await redis.get(key)
        if cur:
            s = cur if isinstance(cur, str) else cur.decode("utf-8", errors="replace")
            data = json.loads(s)
            if isinstance(data, dict) and data.get("status") == "approved":
                return True
    except Exception as e:
        logger.debug("request_approval re-read: %s", e)
    return False


async def approval_status(ctx: Any, token: str) -> str | None:
    """Trả về status string ('pending'|'approved'|'denied') hoặc None nếu hết hạn/không có."""
    redis = getattr(ctx, "redis", None)
    if redis is None:
        return None
    key = f"{APPROVAL_KEY_PREFIX}{token}"
    try:
        cur = await redis.get(key)
        if not cur:
            return None
        s = cur if isinstance(cur, str) else cur.decode("utf-8", errors="replace")
        data = json.loads(s)
        if isinstance(data, dict):
            st = data.get("status")
            return str(st) if st is not None else None
    except Exception as e:
        logger.debug("approval_status: %s", e)
    return None
