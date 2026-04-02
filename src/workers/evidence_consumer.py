"""Consume ``omni-diagnostic-evidence`` and run reasoning (handle_inbound) — no omni-alerts subscription."""

from __future__ import annotations

import json
import logging
from typing import Any

from pkg.reasoning import coerce_evidence_dict
from workers.handlers import WorkerHandlerContext, handle_inbound_payload
from workers.telegram_outbound import send_telegram_out_for_inbound

logger = logging.getLogger(__name__)


async def reason_from_diagnostic_evidence(ctx: WorkerHandlerContext, fields: dict[str, str]) -> str:
    """Turn evidence Kafka record into inbound payload for reasoning-only path."""
    raw = fields.get("data") or "{}"
    try:
        ev_doc = json.loads(raw)
    except Exception:
        ev_doc = {"kind": "parse_error", "raw": raw[:8000]}
    ev_doc = coerce_evidence_dict(ev_doc)
    trace = str(ev_doc.get("trace_id") or "evidence-unknown")
    text = json.dumps(ev_doc, ensure_ascii=False, indent=2)[:12000]
    chat_id: int | None = None
    ctx_blob = await ctx.redis.get(f"omni:evidence_reply:{trace}")
    if ctx_blob:
        try:
            meta = json.loads(ctx_blob.decode() if isinstance(ctx_blob, bytes) else ctx_blob)
            cid = meta.get("chat_id")
            if cid is not None:
                chat_id = int(cid)
        except Exception:
            logger.warning("[%s] evidence_reply context parse failed", trace)
    payload: dict[str, Any] = {
        "trace_id": trace,
        "source": "diagnostic_evidence",
        "text": f"[DIAGNOSTIC_EVIDENCE]\n{text}",
    }
    if chat_id is not None:
        payload["chat_id"] = chat_id
    out = await handle_inbound_payload(ctx, payload)
    if chat_id is not None:
        await send_telegram_out_for_inbound(ctx, payload, trace, out)
    return out
