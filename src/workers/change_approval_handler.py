"""Xử lý Telegram callback change_approve:{id} / change_reject:{id}.

Admin nhận card topology change → bấm Approve/Reject → Omni ghi kết quả vào Redis.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_CHANGE_PENDING_PREFIX = "omni:knowledge:change_pending:"


async def handle_change_approval_callback(
    ctx: Any,  # WorkerHandlerContext
    u: dict[str, Any],
) -> bool:
    """Xử lý callback_data bắt đầu bằng change_approve: hoặc change_reject:.

    Trả True nếu đã handle (để caller skip các handler khác), False nếu không phải callback này.
    """
    cb = u.get("callback_query")
    if not isinstance(cb, dict):
        return False
    data = (cb.get("data") or "").strip()
    if not (data.startswith("change_approve:") or data.startswith("change_reject:")):
        return False

    cq_id = str(cb.get("id") or "")
    approved = data.startswith("change_approve:")
    change_id = data.split(":", 1)[1]

    try:
        key = f"{_CHANGE_PENDING_PREFIX}{change_id}"
        raw = await ctx.redis.get(key)
        if not raw:
            if ctx.telegram and cq_id:
                await ctx.telegram.answer_callback_query(cq_id, text="Đã hết hạn hoặc không tồn tại")
            return True

        record = json.loads(raw)
        record["decision"] = "approved" if approved else "rejected"
        record["decided_by"] = "telegram_admin"

        # Ghi lại quyết định (overwrite với TTL giảm còn 1h)
        await ctx.redis.set(key, json.dumps(record, ensure_ascii=False), ex=3600)

        if approved:
            logger.info(
                "change_approved change_id=%s entity=%s type=%s",
                change_id,
                record.get("entity_name"),
                record.get("change_type"),
            )
            reply = f"✅ Đã duyệt thay đổi: {record.get('change_type')} {record.get('entity_name')}"
        else:
            logger.info(
                "change_rejected change_id=%s entity=%s type=%s",
                change_id,
                record.get("entity_name"),
                record.get("change_type"),
            )
            reply = f"❌ Đã từ chối: {record.get('change_type')} {record.get('entity_name')}"

        if ctx.telegram and cq_id:
            await ctx.telegram.answer_callback_query(cq_id, text=reply[:200])

    except Exception:
        logger.exception("change_approval_callback change_id=%s", change_id)
        if ctx.telegram and cq_id:
            try:
                await ctx.telegram.answer_callback_query(cq_id, text="Lỗi xử lý")
            except Exception:
                pass

    return True
