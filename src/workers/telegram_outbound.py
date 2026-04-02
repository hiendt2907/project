"""Telegram outbound replies — shared by omni_worker and evidence consumer."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from workers.handlers import WorkerHandlerContext

logger = logging.getLogger(__name__)


async def send_telegram_out_for_inbound(
    ctx: WorkerHandlerContext,
    payload: dict[str, Any],
    trace: str,
    out: str,
) -> None:
    if not ctx.telegram or payload.get("chat_id") is None:
        return
    cid = int(payload["chat_id"])
    if ctx.settings.reply_append_trace_id:
        tid = str(payload.get("trace_id") or trace or "").strip()
        if tid:
            out = f"{out.rstrip()}\n\ntrace_id={tid}"[:4000]
    fb = getattr(ctx, "fallback_inline_commands", None)
    if (
        fb
        and len(fb) == 3
        and ctx.settings.fallback_inline_buttons_enabled
    ):
        h = hashlib.sha256(trace.encode()).hexdigest()[:16]
        await ctx.redis.setex(f"omni:fb_h:{h}", 86400, trace)
        await ctx.redis.setex(
            f"omni:fb_suggest:{trace}",
            86400,
            json.dumps(fb, ensure_ascii=False),
        )
        rows: list[list[dict[str, str]]] = []
        for i, cmd in enumerate(fb):
            label = cmd if len(cmd) <= 64 else (cmd[:61] + "…")
            rows.append([{"text": label, "callback_data": f"ofs:{h}:{i}"}])
        await ctx.telegram.send_message(cid, out[:4000], reply_markup={"inline_keyboard": rows})
    else:
        await ctx.telegram.send_message(cid, out[:4000])
    ctx.fallback_inline_commands = None
