"""Telegram HITL 2 chiều — inline card + callback ``hitl:*`` (MASTER_PLAN §4).

1 kênh operator. Mutate cần duyệt (MEDIUM@assist, hoặc HIGH mọi tier) → render card
[✅ Approve][❌ Reject] callback_data=``hitl:{decision}:{pending_id}``. Operator bấm
→ handle_hitl_callback: CRAT HITL_DECISION ghi TRƯỚC khi dispatch (fail-closed) →
APPROVED→omni-actions, REJECTED→omni-action-feedback → answer_callback_query.

Pending payload lưu Redis ``omni:hitl:pending:{id}`` (TTL escalation). Timeout
auto-reject xử lý ở hitl_dispatcher (đã có) — module này lo đường Telegram.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from services.audit_ledger.chain_writer import write_audit_block
from services.audit_ledger.crat_event_types import CRAT_EVENT_HITL_DECISION
from services.audit_ledger.signer import AuditLedgerError

logger = logging.getLogger(__name__)

_PENDING_PREFIX = "omni:hitl:pending:"
_CALLBACK_PREFIX = "hitl:"
APPROVE = "approve"
REJECT = "reject"


def pending_key(pending_id: str) -> str:
    return f"{_PENDING_PREFIX}{pending_id}"


def build_hitl_card(
    *,
    pending_id: str,
    tool_name: str,
    risk_class: str,
    tier: str,
    reason: str = "",
    explain: str = "",
) -> tuple[str, dict[str, Any]]:
    """Trả (text, reply_markup) cho card duyệt. callback_data ``hitl:{decision}:{id}``."""
    lines = [
        "🔒 <b>HITL — Yêu cầu duyệt hành động</b>",
        f"• Tool: <code>{tool_name}</code>",
        f"• Risk: <b>{risk_class}</b>  |  Tier: <b>{tier}</b>",
    ]
    if reason:
        lines.append(f"• Lý do: {reason}")
    if explain:
        lines.append(f"• Phân tích: {explain[:300]}")
    lines.append(f"• ID: <code>{pending_id}</code>")
    text = "\n".join(lines)
    reply_markup = {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"{_CALLBACK_PREFIX}{APPROVE}:{pending_id}"},
            {"text": "❌ Reject", "callback_data": f"{_CALLBACK_PREFIX}{REJECT}:{pending_id}"},
        ]]
    }
    return text, reply_markup


def parse_hitl_callback(data: str) -> tuple[str, str] | None:
    """``hitl:approve:{id}`` → (decision, pending_id). None nếu không phải hitl callback."""
    if not data.startswith(_CALLBACK_PREFIX):
        return None
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[1] not in (APPROVE, REJECT) or not parts[2]:
        return None
    return parts[1], parts[2]


async def dispatch_hitl_ui_decision(ctx: Any, decision_msg: dict[str, Any]) -> bool:
    """Dispatch quyết định HITL đến từ Admin UI (topic ``omni-hitl-decisions``).

    Khác đường Telegram: CRAT HITL_DECISION đã được gateway enqueue atomic vào
    ``omni_admin.crat_outbox`` (drainer ghi block) → ở đây CHỈ định tuyến Kafka +
    dọn pending. APPROVED→omni-actions, REJECTED→omni-action-feedback. Idempotent:
    pending đã xoá → no-op. Trả True nếu xử lý (kể cả no-op).
    """
    pending_id = str(decision_msg.get("pending_id") or "").strip()
    decision = str(decision_msg.get("decision") or "").strip()
    if not pending_id or decision not in ("APPROVED", "REJECTED"):
        logger.warning("hitl_ui: bỏ qua message không hợp lệ: %s", decision_msg)
        return True
    settings = ctx.settings
    redis = getattr(ctx, "redis", None)
    kafka = getattr(ctx, "kafka", None)
    actor = str(decision_msg.get("actor") or "admin_ui")
    tenant_id = str(decision_msg.get("tenant_id") or "default")

    pending: dict[str, Any] = {}
    if redis is not None:
        try:
            raw = await redis.get(pending_key(pending_id))
            if raw:
                pending = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hitl_ui: load pending fail id=%s err=%s", pending_id, exc)
    trace = str(pending.get("trace_id") or pending.get("trace") or f"hitl-{pending_id}")

    if kafka is not None:
        if decision == "APPROVED":
            action_topic = getattr(settings, "kafka_topic_actions", "omni-actions")
            body = dict(pending.get("action_body") or {})
            body.update({"trace_id": trace, "hitl_decision": "APPROVED",
                         "hitl_actor": actor, "hitl_channel": "ui"})
            await kafka.send_dict(action_topic, body)
        else:
            fb_topic = getattr(settings, "kafka_topic_action_feedback", "omni-action-feedback")
            await kafka.send_dict(fb_topic, {
                "trace_id": trace, "pending_id": pending_id, "outcome": "rejected",
                "hitl_actor": actor, "hitl_channel": "ui",
                "tool_name": decision_msg.get("tool_name") or pending.get("tool_name", ""),
            })

    if redis is not None:
        try:
            await redis.delete(pending_key(pending_id))
        except Exception:  # noqa: BLE001
            pass
    logger.info("hitl_ui: dispatched decision=%s id=%s actor=%s tenant=%s",
                decision, pending_id, actor, tenant_id)
    return True


async def handle_hitl_callback(ctx: Any, update: dict[str, Any]) -> bool:
    """Xử lý callback ``hitl:*``. Trả True nếu đã tiêu thụ update (kể cả lỗi).

    Bất biến: CRAT HITL_DECISION ghi TRƯỚC dispatch (fail-closed). CRAT fail →
    KHÔNG dispatch, báo lỗi cho operator.
    """
    cb = update.get("callback_query")
    if not isinstance(cb, dict):
        return False
    data = (cb.get("data") or "").strip()
    parsed = parse_hitl_callback(data)
    if parsed is None:
        return False
    decision_raw, pending_id = parsed
    cq_id = str(cb.get("id") or "")
    tg = getattr(ctx, "telegram", None)
    redis = getattr(ctx, "redis", None)
    kafka = getattr(ctx, "kafka", None)
    settings = getattr(ctx, "settings", None)

    # Nạp pending context (best-effort; thiếu vẫn duyệt được theo id).
    pending: dict[str, Any] = {}
    if redis is not None:
        try:
            raw = await redis.get(pending_key(pending_id))
            if raw:
                pending = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hitl: load pending fail id=%s err=%s", pending_id, exc)

    decision = "APPROVED" if decision_raw == APPROVE else "REJECTED"
    actor = str((cb.get("from") or {}).get("id") or "telegram_operator")
    trace = str(pending.get("trace_id") or pending.get("trace") or f"hitl-{pending_id}")
    tenant_id = str(pending.get("tenant_id") or "default")

    # ── CRAT fail-closed: ghi HITL_DECISION TRƯỚC khi dispatch ──────────────
    audit_topic = getattr(settings, "kafka_topic_audit_chain", "omni-audit-chain")
    try:
        await write_audit_block(
            event_type=CRAT_EVENT_HITL_DECISION,
            trace_id=trace,
            payload={
                "pending_id": pending_id,
                "decision": decision,
                "actor": actor,
                "channel": "telegram",
                "tool_name": pending.get("tool_name", ""),
                "risk_class": pending.get("risk_class", ""),
                "tier": pending.get("tier", ""),
                "tenant_id": tenant_id,
            },
            redis=redis,
            kafka=kafka,
            kafka_topic=audit_topic,
            tenant_id=tenant_id,
        )
    except AuditLedgerError as exc:
        logger.critical("hitl: CRAT write FAILED id=%s — abort dispatch err=%s", pending_id, exc)
        if tg and cq_id:
            await tg.answer_callback_query(cq_id, text="Lỗi audit — không thực thi", show_alert=True)
        return True

    # ── Dispatch theo quyết định ────────────────────────────────────────────
    if kafka is not None:
        if decision == "APPROVED":
            action_topic = getattr(settings, "kafka_topic_actions", "omni-actions")
            body = dict(pending.get("action_body") or {})
            body.update({"trace_id": trace, "hitl_decision": "APPROVED", "hitl_actor": actor})
            await kafka.send_dict(action_topic, body)
        else:
            fb_topic = getattr(settings, "kafka_topic_action_feedback", "omni-action-feedback")
            await kafka.send_dict(fb_topic, {
                "trace_id": trace, "pending_id": pending_id,
                "outcome": "rejected", "hitl_actor": actor,
                "tool_name": pending.get("tool_name", ""),
            })

    # ── Persist ledger row (omni_admin.hitl_decision) nếu repo có ───────────
    repo = getattr(ctx, "admin_repo", None)
    if repo is not None and hasattr(repo, "record_hitl_decision"):
        try:
            await repo.record_hitl_decision(
                pending_id=pending_id, decision=decision, actor=actor, channel="telegram",
            )
        except Exception as exc:  # noqa: BLE001 — ledger phụ trợ, CRAT mới là chain
            logger.warning("hitl: ledger persist fail id=%s err=%s", pending_id, exc)

    # ── Dọn pending + ack operator ─────────────────────────────────────────
    if redis is not None:
        try:
            await redis.delete(pending_key(pending_id))
        except Exception:  # noqa: BLE001
            pass
    if tg and cq_id:
        emoji = "✅ Đã duyệt" if decision == "APPROVED" else "❌ Đã từ chối"
        await tg.answer_callback_query(cq_id, text=emoji)
    logger.info("hitl: decision=%s id=%s actor=%s tool=%s", decision, pending_id, actor, pending.get("tool_name", ""))
    return True
