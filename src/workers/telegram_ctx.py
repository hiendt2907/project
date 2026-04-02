"""Chuẩn hoá chat_id / gửi ảnh từ Telegram — LLM không cần nhớ tham số."""

from __future__ import annotations

from typing import Any


def effective_telegram_chat_id(ctx: Any, args: dict[str, Any]) -> int | None:
    """Ưu tiên args.chat_id; fallback ``ctx.telegram_chat_id`` (handler gán từ payload)."""
    raw = args.get("chat_id")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    tid = getattr(ctx, "telegram_chat_id", None)
    if tid is None:
        return None
    try:
        return int(tid)
    except (TypeError, ValueError):
        return None


def should_send_telegram_chart(ctx: Any, args: dict[str, Any]) -> bool:
    """
    Có chat_id hợp lệ → mặc định gửi chart (trừ khi ``send_telegram: false`` rõ ràng).
    """
    if effective_telegram_chat_id(ctx, args) is None:
        return False
    if args.get("send_telegram") is False:
        return False
    return bool(args.get("send_telegram", True))
