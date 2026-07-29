"""Advisory Mode — durable Kafka log + operator acknowledgment for Telegram suggestions.

Khác `hitl_telegram.py` (mutation-approval, `hitl:*`, disabled trong Advisory Mode):
module này KHÔNG chặn/duyệt mutation nào — chỉ (1) ghi suggestion advisory vào Kafka
durable (`omni-advisory-suggestions`) trước khi gửi Telegram, và (2) khi operator bấm
"Đã ghi nhận" trên Telegram, ghi quyết định ack đó vào CRAT (`ADVISORY_DECISION`) +
Kafka + Postgres (`omni_admin.advisory_acknowledgment`). Namespace callback riêng
(`advack:`), không đụng `hitl:`/`ofs:`/`change_*`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from services.audit_ledger.chain_writer import write_audit_block
from services.audit_ledger.crat_event_types import CRAT_EVENT_ADVISORY_DECISION
from services.audit_ledger.signer import AuditLedgerError

logger = logging.getLogger(__name__)

_CALLBACK_PREFIX = "advack:"
ACKNOWLEDGED = "ACKNOWLEDGED"


def build_advisory_ack_keyboard(trace_id: str) -> dict[str, Any]:
    """reply_markup cho nút ghi nhận trên tin nhắn advisory. Không phải approve/reject —
    Advisory Mode không có mutation nào để duyệt ở đây, chỉ ghi nhận operator đã xem."""
    return {
        "inline_keyboard": [[
            {"text": "✅ Đã ghi nhận", "callback_data": f"{_CALLBACK_PREFIX}{trace_id}"},
        ]]
    }


def parse_advisory_ack_callback(data: str) -> str | None:
    """``advack:{trace_id}`` -> trace_id. None nếu không phải advisory-ack callback."""
    if not data.startswith(_CALLBACK_PREFIX):
        return None
    trace_id = data[len(_CALLBACK_PREFIX):].strip()
    return trace_id or None


async def emit_advisory_suggestion(
    ctx: Any,
    *,
    trace_id: str,
    tenant_id: str,
    advisory_payload: dict[str, Any],
) -> None:
    """Ghi suggestion advisory vào Kafka durable TRƯỚC khi gửi Telegram (best-effort —
    không chặn gửi Telegram nếu Kafka lỗi, vì Telegram vẫn là kênh chính của Advisory Mode)."""
    kafka = getattr(ctx, "kafka", None)
    if kafka is None:
        return
    settings = getattr(ctx, "settings", None)
    topic = getattr(settings, "kafka_topic_advisory_suggestions", "omni-advisory-suggestions")
    body = {
        "trace_id": trace_id,
        "tenant_id": tenant_id,
        "status": "pending_ack",
        "advisory": advisory_payload,
    }
    try:
        await kafka.send_dict(topic, {"data": json.dumps(body, ensure_ascii=False, default=str)})
    except Exception as exc:  # noqa: BLE001 — durable log best-effort, Telegram vẫn phải gửi
        logger.warning("advisory_ack: emit suggestion fail trace=%s err=%s", trace_id, exc)


_TRACE_ADVISORY_KEY = "omni:trace:advisory:"


async def _load_advisory_shape(redis: Any, trace_id: str) -> dict[str, Any]:
    """Đọc ``lane``/``alertname`` của trace để gom nhóm pattern khi tốt nghiệp.

    Nguồn là `omni:trace:advisory:{trace}` do `remote_agent_pipeline._persist_trace_advisory`
    ghi (TTL 1h). Trả dict rỗng nếu không có — `advisory_pattern_key` sẽ coi là bỏ qua,
    không đoán bừa một pattern sai.
    """
    if redis is None or not trace_id:
        return {}
    try:
        raw = await redis.get(f"{_TRACE_ADVISORY_KEY}{trace_id}")
    except Exception:  # noqa: BLE001
        return {}
    if not raw:
        return {}
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    advisory = doc.get("advisory") if isinstance(doc.get("advisory"), dict) else {}
    workload = advisory.get("affected_workload")
    alertname = ""
    if isinstance(workload, dict):
        alertname = str(workload.get("alertname") or workload.get("name") or "")
    return {
        "lane": str(doc.get("lane") or ""),
        "alertname": alertname or str(advisory.get("verdict") or ""),
    }


async def handle_advisory_ack_callback(ctx: Any, update: dict[str, Any]) -> bool:
    """Xử lý callback ``advack:{trace_id}``. Trả True nếu đã tiêu thụ update.

    Fail-closed cho CRAT: nếu ghi ADVISORY_DECISION lỗi, báo operator lỗi và KHÔNG
    coi là đã ghi nhận (không có gì để "dispatch" ở đây — khác hitl_telegram, không
    có mutation nào bị treo chờ quyết định này).
    """
    cb = update.get("callback_query")
    if not isinstance(cb, dict):
        return False
    data = (cb.get("data") or "").strip()
    trace_id = parse_advisory_ack_callback(data)
    if trace_id is None:
        return False

    cq_id = str(cb.get("id") or "")
    tg = getattr(ctx, "telegram", None)
    redis = getattr(ctx, "redis", None)
    kafka = getattr(ctx, "kafka", None)
    settings = getattr(ctx, "settings", None)
    actor = str((cb.get("from") or {}).get("id") or "telegram_operator")
    tenant_id = "default"

    audit_topic = getattr(settings, "kafka_topic_audit_chain", "omni-audit-chain")
    try:
        await write_audit_block(
            event_type=CRAT_EVENT_ADVISORY_DECISION,
            trace_id=trace_id,
            payload={
                "decision": ACKNOWLEDGED,
                "actor": actor,
                "channel": "telegram",
                "tenant_id": tenant_id,
            },
            redis=redis,
            kafka=kafka,
            kafka_topic=audit_topic,
            tenant_id=tenant_id,
        )
    except AuditLedgerError as exc:
        logger.critical("advisory_ack: CRAT write FAILED trace=%s err=%s", trace_id, exc)
        if tg and cq_id:
            await tg.answer_callback_query(cq_id, text="Lỗi audit — chưa ghi nhận", show_alert=True)
        return True

    if kafka is not None:
        topic = getattr(settings, "kafka_topic_advisory_suggestions", "omni-advisory-suggestions")
        try:
            await kafka.send_dict(topic, {"data": json.dumps({
                "trace_id": trace_id,
                "tenant_id": tenant_id,
                "status": "acknowledged",
                "actor": actor,
            }, ensure_ascii=False)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("advisory_ack: emit ack fail trace=%s err=%s", trace_id, exc)

    repo = getattr(ctx, "admin_repo", None)
    if repo is not None and hasattr(repo, "record_advisory_acknowledgment"):
        try:
            await repo.record_advisory_acknowledgment(
                trace_id=trace_id, actor=actor, channel="telegram", tenant_id=tenant_id,
            )
        except Exception as exc:  # noqa: BLE001 — ledger phụ trợ, CRAT mới là chain
            logger.warning("advisory_ack: ledger persist fail trace=%s err=%s", trace_id, exc)

    # G2 — ack là mẫu KPI acceptance. Không có nguồn nào khác điền `omni:kpi:z:*:accepted`
    # trong shadow mode, và đó chính là bằng chứng dùng để xét nâng tier sau này.
    if redis is not None:
        try:
            from workers.kpi_metrics import KPIStore

            await KPIStore(redis).record_accepted(trace_id, tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("advisory_ack: kpi record fail trace=%s err=%s", trace_id, exc)

    # G1 — vòng học: ack của operator là tín hiệu học DUY NHẤT có thật trong shadow mode
    # (không mutation → không VERIFIED_SUCCESS → promoter.evaluate_for_promotion không
    # bao giờ chạy). Best-effort: học hỏng KHÔNG được làm hỏng việc ghi nhận advisory.
    try:
        from services.learning_promoter.advisory_promoter import record_advisory_verdict

        await record_advisory_verdict(
            ctx,
            tenant_id=tenant_id,
            trace_id=trace_id,
            accepted=True,
            advisory=await _load_advisory_shape(redis, trace_id),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("advisory_ack: graduation skip trace=%s err=%s", trace_id, exc)

    if tg and cq_id:
        await tg.answer_callback_query(cq_id, text="✅ Đã ghi nhận")
    logger.info("advisory_ack: acknowledged trace=%s actor=%s", trace_id, actor)
    return True
