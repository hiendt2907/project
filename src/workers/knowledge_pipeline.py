"""Knowledge pipeline — xử lý omni-knowledge-evidence (METRIC_SAMPLE/LOG_SAMPLE/DISCOVERY/CHANGE_DETECTED).

INV_KNOWLEDGE_NOT_ALERT: không có RAG lookup, không có LLM call, không emit alert.
Pipeline này thu thập, tích lũy, và hỏi admin khi cần — không tạo incident.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from anomaly.remote_host_baseline import update_remote_host_baseline, add_confidence
from workers.handler_context import WorkerHandlerContext

logger = logging.getLogger(__name__)

# Rolling log store per agent: omni:knowledge:logs:{agent_id}:rolling
_LOG_STORE_PREFIX = "omni:knowledge:logs:"
_LOG_STORE_SUFFIX = ":rolling"
_LOG_STORE_MAX = 500
_LOG_STORE_TTL = 86400  # 24h

# Pending change approvals: omni:knowledge:change_pending:{tenant}:{change_id}
_CHANGE_PENDING_PREFIX = "omni:knowledge:change_pending:"
_CHANGE_PENDING_TTL = 7 * 86400  # 7d


async def handle_knowledge_evidence(ctx: WorkerHandlerContext, ev_doc: dict[str, Any]) -> None:
    """Dispatcher theo signal_type. Called từ kafka_knowledge_evidence_loop."""
    signal_type = str(ev_doc.get("signal_type") or "UNKNOWN")
    tenant_id = str(ev_doc.get("tenant_id") or "default")
    agent_id = str(ev_doc.get("extracted_fact", {}).get("agent_id") or "unknown")
    hostname = str(ev_doc.get("namespace") or ev_doc.get("extracted_fact", {}).get("hostname") or agent_id)

    if signal_type == "METRIC_SAMPLE":
        await _handle_metric_sample(ctx, ev_doc, tenant_id, hostname)
    elif signal_type == "LOG_SAMPLE":
        await _handle_log_sample(ctx, ev_doc, agent_id)
    elif signal_type == "DISCOVERY":
        await _handle_discovery(ctx, ev_doc, tenant_id, agent_id, hostname)
    elif signal_type == "CHANGE_DETECTED":
        await _handle_change_detected(ctx, ev_doc, tenant_id, hostname)
    elif signal_type == "UNKNOWN_ENTITY":
        await _handle_unknown_entity(ctx, ev_doc, tenant_id, hostname)
    else:
        logger.debug("knowledge_pipeline: unknown signal_type=%s agent=%s", signal_type, agent_id)


async def _handle_metric_sample(
    ctx: WorkerHandlerContext,
    ev_doc: dict[str, Any],
    tenant_id: str,
    hostname: str,
) -> None:
    """METRIC_SAMPLE → update 3σ baseline + add confidence."""
    fact = ev_doc.get("extracted_fact") or {}
    if not isinstance(fact, dict):
        return

    try:
        zscores = await update_remote_host_baseline(
            ctx.redis,
            tenant_id=tenant_id,
            host=hostname,
            fact=fact,
        )
    except Exception as exc:
        logger.warning("knowledge_pipeline: metric_sample baseline err host=%s err=%s", hostname, exc)
        return

    # +1 confidence per 100 samples (tracked via a simple counter)
    counter_key = f"omni:knowledge:metric_count:{tenant_id}:{hostname}"
    try:
        count = await ctx.redis.incr(counter_key)
        await ctx.redis.expire(counter_key, 90 * 86400)  # 90d TTL
        if count % 100 == 0:
            await add_confidence(ctx.redis, tenant_id=tenant_id, host=hostname, delta=1)
            logger.info(
                "knowledge_pipeline: metric_milestone tenant=%s host=%s samples=%d",
                tenant_id, hostname, count,
            )
    except Exception as exc:
        logger.debug("knowledge_pipeline: metric confidence incr err=%s", exc)

    if zscores:
        logger.debug("knowledge_pipeline: metric_sample host=%s zscores=%s", hostname, zscores)


async def _handle_log_sample(
    ctx: WorkerHandlerContext,
    ev_doc: dict[str, Any],
    agent_id: str,
) -> None:
    """LOG_SAMPLE → rolling log store (RAG context for future queries)."""
    key = f"{_LOG_STORE_PREFIX}{agent_id}{_LOG_STORE_SUFFIX}"
    entry = {
        "ts": ev_doc.get("ts") or str(int(time.time())),
        "alert_hint": (ev_doc.get("alert_hint") or "")[:500],
        "extracted_fact": ev_doc.get("extracted_fact") or {},
        "raw": (ev_doc.get("raw") or "")[:500],
    }
    try:
        await ctx.redis.lpush(key, json.dumps(entry, ensure_ascii=False))
        await ctx.redis.ltrim(key, 0, _LOG_STORE_MAX - 1)
        await ctx.redis.expire(key, _LOG_STORE_TTL)
    except Exception as exc:
        logger.debug("knowledge_pipeline: log_sample store err agent=%s err=%s", agent_id, exc)


async def _handle_discovery(
    ctx: WorkerHandlerContext,
    ev_doc: dict[str, Any],
    tenant_id: str,
    agent_id: str,
    hostname: str,
) -> None:
    """DISCOVERY → diff với baseline → emit CHANGE_DETECTED nếu có thay đổi."""
    from remote_agent.discovery import (
        save_discovery_snapshot,
        load_discovery_snapshot,
        diff_discovery,
        is_snapshot_suspect,
        bump_suspect_streak,
        reset_suspect_streak,
        suspect_confirm_threshold,
    )

    probe = str(ev_doc.get("probe") or "unknown")
    fact = ev_doc.get("extracted_fact") or {}
    discovery_data = fact.get("discovery_data") if isinstance(fact, dict) else None
    if not isinstance(discovery_data, dict):
        return

    # Services snapshot (từ service_topology probe) — compare và detect changes.
    # KHÔNG bọc try/except nuốt lỗi ở đây: một lỗi Redis đọc/ghi thật (không
    # phải "key chưa tồn tại", xem load_discovery_snapshot) phải văng ra tới
    # caller (kafka_knowledge_evidence_loop) để đi qua retry+poison-ack sẵn
    # có, thay vì bị nuốt âm thầm khiến chu kỳ diff đó biến mất không dấu vết.
    if probe == "service_topology":
        new_snapshot = discovery_data
        old_snapshot = await load_discovery_snapshot(ctx.redis, tenant_id=tenant_id, agent_id=agent_id)
        suspect = old_snapshot is not None and is_snapshot_suspect(old_snapshot, new_snapshot)
        if suspect:
            threshold = suspect_confirm_threshold()
            streak = await bump_suspect_streak(ctx.redis, tenant_id=tenant_id, agent_id=agent_id)
            if streak < threshold:
                # 1 chu kỳ rỗng bất thường có thể là collector blip thoáng qua
                # (systemctl timeout/dbus hiccup) — KHÔNG diff, KHÔNG ghi đè
                # baseline, tránh làm hỏng vĩnh viễn system model từ 1 lần lỗi.
                logger.warning(
                    "knowledge_pipeline: discovery snapshot suspect (services=0, "
                    "prev=%d, streak=%d/%d) tenant=%s host=%s — skip diff+baseline overwrite",
                    len(old_snapshot.get("services", [])), streak, threshold,
                    tenant_id, hostname,
                )
                return
            # Xác nhận qua >=2 chu kỳ liên tiếp — chấp nhận là thật (vd. toàn bộ
            # service trên host thật sự down), không còn coi là collector blip.
            await reset_suspect_streak(ctx.redis, tenant_id=tenant_id, agent_id=agent_id)
        elif old_snapshot is not None:
            await reset_suspect_streak(ctx.redis, tenant_id=tenant_id, agent_id=agent_id)

        if old_snapshot is not None:
            changes = diff_discovery(old_snapshot, new_snapshot)
            for change in changes:
                await _emit_change_detected(ctx, tenant_id, hostname, change, agent_id)
        # Lưu snapshot mới (baseline cập nhật). Đặt SAU khi diff+emit đã xong
        # để 1 retry (do bước forward bên dưới lỗi) load lại baseline == snapshot
        # mới, diff ra rỗng — không phát trùng change-detected/Telegram.
        await save_discovery_snapshot(ctx.redis, tenant_id=tenant_id, agent_id=agent_id, snapshot=new_snapshot)

    # Forward to omni-discovery-evidence so onboarding worker accumulates facts.
    # Không nuốt lỗi: 1 lần Kafka chập chờn đúng lúc forward trước đây làm
    # evidence biến mất vĩnh viễn (offset nguồn đã commit ngay sau khi hàm
    # này return không lỗi) — để lỗi văng ra cho retry+poison-ack xử lý.
    kafka = getattr(ctx, "kafka", None)
    if kafka is not None:
        discovery_topic = getattr(
            ctx.settings, "kafka_topic_discovery_evidence", "omni-discovery-evidence"
        )
        trace = str(ev_doc.get("trace_id") or agent_id)
        await kafka.send_dict(
            discovery_topic,
            {"data": json.dumps(ev_doc, ensure_ascii=False)},
            key=trace.encode("utf-8", errors="ignore"),
        )
        logger.info(
            "knowledge_pipeline: discovery forwarded tenant=%s probe=%s topic=%s",
            tenant_id, probe, discovery_topic,
        )

    logger.debug("knowledge_pipeline: discovery probe=%s agent=%s", probe, agent_id)


async def _emit_change_detected(
    ctx: WorkerHandlerContext,
    tenant_id: str,
    hostname: str,
    change: dict[str, Any],
    agent_id: str,
) -> None:
    """Lưu change pending + gửi Telegram inline keyboard approve/reject."""
    change_id = uuid.uuid4().hex[:12]
    pending_key = f"{_CHANGE_PENDING_PREFIX}{tenant_id}:{change_id}"
    change_type = change.get("change_type", "UNKNOWN")
    entity_name = change.get("entity_name", "unknown")
    entity_type = change.get("entity_type", "service")

    pending = {
        "change_id": change_id,
        "tenant_id": tenant_id,
        "hostname": hostname,
        "agent_id": agent_id,
        "change_type": change_type,
        "entity_type": entity_type,
        "entity_name": entity_name,
        "old_value": change.get("old_value", ""),
        "new_value": change.get("new_value", ""),
        "ts": str(int(time.time())),
        "status": "pending",
    }
    try:
        await ctx.redis.set(pending_key, json.dumps(pending), ex=_CHANGE_PENDING_TTL)
    except Exception as exc:
        logger.warning("knowledge_pipeline: change_pending store err change_id=%s err=%s", change_id, exc)

    if ctx.telegram is None or not ctx.telegram_chat_id:
        logger.info(
            "knowledge_pipeline: change_detected (no telegram) tenant=%s host=%s type=%s entity=%s",
            tenant_id, hostname, change_type, entity_name,
        )
        return

    _CHANGE_ICONS = {
        "SERVICE_ADDED": ("🔍", "Service mới xuất hiện"),
        "SERVICE_REMOVED": ("⚠️", "Service biến mất"),
        "PORT_OPENED": ("🔓", "Port mới mở"),
        "PORT_CLOSED": ("🔒", "Port đóng"),
    }
    icon, label = _CHANGE_ICONS.get(change_type, ("❓", change_type))
    text = (
        f"{icon} <b>{label}</b> trên <code>{hostname}</code>\n"
        f"Thực thể: <code>{entity_type}</code> / <code>{entity_name}</code>\n"
        f"Trước: {change.get('old_value') or '(trống)'} → Sau: {change.get('new_value') or '(trống)'}\n\n"
        f"Approve để cập nhật baseline. Reject nếu không mong đợi."
    )

    reply_markup = {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"change_approve:{change_id}"},
            {"text": "❌ Reject", "callback_data": f"change_reject:{change_id}"},
        ]]
    }

    try:
        await ctx.telegram.send_message(
            chat_id=ctx.telegram_chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        logger.info(
            "knowledge_pipeline: change_telegram_sent change_id=%s host=%s type=%s entity=%s",
            change_id, hostname, change_type, entity_name,
        )
    except Exception as exc:
        logger.warning("knowledge_pipeline: telegram send err change_id=%s err=%s", change_id, exc)


async def _handle_change_detected(
    ctx: WorkerHandlerContext,
    ev_doc: dict[str, Any],
    tenant_id: str,
    hostname: str,
) -> None:
    """CHANGE_DETECTED envelope (từ agent emit trực tiếp) — delegate đến change pending flow."""
    fact = ev_doc.get("extracted_fact") or {}
    if not isinstance(fact, dict):
        return
    agent_id = str(fact.get("agent_id") or "unknown")
    change = {
        "change_type": str(fact.get("change_type") or "UNKNOWN"),
        "entity_type": str(fact.get("entity_type") or "service"),
        "entity_name": str(fact.get("entity_name") or fact.get("service_name") or "unknown"),
        "old_value": str(fact.get("old_value") or ""),
        "new_value": str(fact.get("new_value") or ""),
    }
    await _emit_change_detected(ctx, tenant_id, hostname, change, agent_id)


async def _handle_unknown_entity(
    ctx: WorkerHandlerContext,
    ev_doc: dict[str, Any],
    tenant_id: str,
    hostname: str,
) -> None:
    """UNKNOWN_ENTITY → Telegram hỏi admin, lưu pending question."""
    fact = ev_doc.get("extracted_fact") or {}
    if not isinstance(fact, dict):
        return

    entity_type = str(fact.get("entity_type") or "process")
    entity_name = str(fact.get("entity_name") or "unknown")
    port = fact.get("port")

    if ctx.telegram is None or not ctx.telegram_chat_id:
        logger.info(
            "knowledge_pipeline: unknown_entity (no telegram) host=%s entity=%s:%s",
            hostname, entity_type, entity_name,
        )
        return

    msg_id_key = f"omni:knowledge:pending_q:{tenant_id}:{uuid.uuid4().hex[:12]}"
    question = (
        f"❓ <b>Thực thể không rõ</b> trên <code>{hostname}</code>\n"
        f"Loại: <code>{entity_type}</code>  Tên: <code>{entity_name}</code>"
        + (f"  Port: <code>{port}</code>" if port else "")
        + "\n\nBạn mô tả service này là gì? Gửi tài liệu (PDF/ảnh) hoặc text trả lời tin nhắn này."
    )
    try:
        sent = await ctx.telegram.send_message(
            chat_id=ctx.telegram_chat_id,
            text=question,
            parse_mode="HTML",
        )
        msg_id = sent.get("result", {}).get("message_id") if isinstance(sent, dict) else None
        if msg_id:
            pending = {
                "question": question,
                "hostname": hostname,
                "entity_type": entity_type,
                "entity_name": entity_name,
                "ts": str(int(time.time())),
            }
            await ctx.redis.set(msg_id_key, json.dumps(pending), ex=7 * 86400)
            # Also index by Telegram message_id for reply-detection in handlers.py
            await ctx.redis.set(
                f"omni:knowledge:pending_q_by_msgid:{ctx.telegram_chat_id}:{msg_id}",
                msg_id_key,
                ex=7 * 86400,
            )
    except Exception as exc:
        logger.warning("knowledge_pipeline: unknown_entity telegram err host=%s err=%s", hostname, exc)


async def handle_telegram_doc_upload(ctx: WorkerHandlerContext, u: dict[str, Any]) -> bool:
    """Phát hiện admin reply bằng tài liệu (document/photo) → ingest vào knowledge store.

    Trả True nếu đã xử lý (caller skip), False nếu không phải doc upload.
    """
    msg = u.get("message") or u.get("edited_message")
    if not isinstance(msg, dict):
        return False

    doc = msg.get("document")
    photo_list = msg.get("photo")
    if doc is None and not photo_list:
        return False

    # Chỉ xử lý nếu là reply cho một pending_q
    reply_to = msg.get("reply_to_message") or {}
    reply_msg_id = reply_to.get("message_id")
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")

    if not reply_msg_id or not chat_id:
        return False

    q_ref_key = f"omni:knowledge:pending_q_by_msgid:{chat_id}:{reply_msg_id}"
    try:
        q_key = await ctx.redis.get(q_ref_key)
    except Exception:
        return False
    if not q_key:
        return False  # Reply cho tin nhắn khác — bỏ qua

    # Xác định file_id và file_name
    if doc:
        file_id = doc.get("file_id", "")
        file_name = doc.get("file_name") or "document"
        mime = doc.get("mime_type") or ""
    else:
        # photo: lấy ảnh lớn nhất
        largest = max(photo_list, key=lambda p: p.get("file_size", 0))
        file_id = largest.get("file_id", "")
        file_name = "photo.jpg"
        mime = "image/jpeg"

    caption = (msg.get("caption") or "").strip()[:2000] or f"[{mime or 'file'}] {file_name}"

    from services.knowledge.document_store import ingest_customer_knowledge
    from anomaly.remote_host_baseline import add_confidence

    try:
        # Lấy context từ pending question
        q_raw = await ctx.redis.get(q_key)
        q_data = json.loads(q_raw) if q_raw else {}
        tenant_id = str(q_data.get("tenant_id") or "default")
        agent_id = str(q_data.get("agent_id") or "unknown")
        hostname = str(q_data.get("hostname") or agent_id)

        doc_id = await ingest_customer_knowledge(
            ctx.redis,
            tenant_id=tenant_id,
            agent_id=agent_id,
            file_id=file_id,
            file_name=file_name,
            summary=caption,
            uploaded_by="telegram_admin",
        )

        # +20 confidence cho việc upload tài liệu (dữ liệu có giá trị cao)
        await add_confidence(ctx.redis, tenant_id=tenant_id, host=hostname, delta=20)

        # Xoá pending question sau khi xử lý
        await ctx.redis.delete(q_key, q_ref_key)

        if ctx.telegram and chat_id:
            await ctx.telegram.send_message(
                chat_id=chat_id,
                text=f"✅ Đã lưu tài liệu <code>{file_name}</code> cho host <code>{hostname}</code> (doc_id: {doc_id[:16]}…)",
                parse_mode="HTML",
            )
        logger.info(
            "knowledge_pipeline: doc_uploaded doc_id=%s tenant=%s host=%s",
            doc_id, tenant_id, hostname,
        )
    except Exception as exc:
        logger.warning("knowledge_pipeline: doc_upload err file=%s err=%r", file_name, exc)

    return True
