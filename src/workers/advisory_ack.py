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

# Phán quyết của NGƯỜI về chẩn đoán. Trước đây chỉ có một nút "đã ghi nhận" và hệ thống
# học từ đó như thể là đồng tình — tức là học từ SỰ CHÚ Ý chứ không phải SỰ ĐỒNG TÌNH.
# Ba nút này tách hai thứ đó ra; nhánh INCORRECT là nhánh duy nhất có thể đóng băng một
# pattern sai, trước đây không đường nào chạm tới được.
VERDICT_CORRECT = "CORRECT"
VERDICT_INCORRECT = "INCORRECT"
VERDICT_PARTIAL = "PARTIAL"

# Token ngắn vì callback_data của Telegram giới hạn 64 byte và trace_id đã chiếm ~36.
_VERDICT_BY_TOKEN: dict[str, str] = {
    "ok": VERDICT_CORRECT,
    "bad": VERDICT_INCORRECT,
    "part": VERDICT_PARTIAL,
}
_ANSWER_TEXT: dict[str, str] = {
    VERDICT_CORRECT: "✅ Đã ghi: chẩn đoán ĐÚNG",
    VERDICT_INCORRECT: "❌ Đã ghi: chẩn đoán SAI",
    VERDICT_PARTIAL: "🟡 Đã ghi: đúng nhưng thiếu",
}


def build_advisory_ack_keyboard(trace_id: str) -> dict[str, Any]:
    """reply_markup phán quyết chẩn đoán cho tin nhắn advisory.

    Vẫn KHÔNG phải approve/reject mutation (Advisory Mode không có mutation nào treo ở
    đây) — đây là phán quyết về chất lượng chẩn đoán, nguồn nhãn học duy nhất có thật
    trong shadow mode. Không bấm gì thì KHÔNG có nhãn nào được ghi: im lặng không phải
    đồng ý, và cố ý không có timeout nào tự coi là đúng.
    """
    return {
        "inline_keyboard": [[
            {"text": "✅ Đúng", "callback_data": f"{_CALLBACK_PREFIX}ok:{trace_id}"},
            {"text": "❌ Sai", "callback_data": f"{_CALLBACK_PREFIX}bad:{trace_id}"},
            {"text": "🟡 Đúng nhưng thiếu", "callback_data": f"{_CALLBACK_PREFIX}part:{trace_id}"},
        ]]
    }


def parse_advisory_verdict_callback(data: str) -> tuple[str, str | None] | None:
    """``advack:{token}:{trace_id}`` -> (trace_id, verdict).

    Vẫn nhận dạng ``advack:{trace_id}`` (dạng cũ, một nút) và trả verdict=None cho nó —
    các tin nhắn phát trước khi đổi bàn phím vẫn còn sống trong lịch sử chat, bấm vào
    không được ném lỗi; nhưng cũng không được suy ra một phán quyết không ai đưa.
    """
    if not data.startswith(_CALLBACK_PREFIX):
        return None
    rest = data[len(_CALLBACK_PREFIX):].strip()
    if not rest:
        return None
    token, sep, tail = rest.partition(":")
    if sep and token in _VERDICT_BY_TOKEN and tail.strip():
        return tail.strip(), _VERDICT_BY_TOKEN[token]
    return rest, None


def parse_advisory_ack_callback(data: str) -> str | None:
    """``advack:...`` -> trace_id. None nếu không phải advisory-ack callback."""
    parsed = parse_advisory_verdict_callback(data)
    return parsed[0] if parsed else None


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


def _case_store(ctx: Any) -> Any | None:
    """CaseLedgerStore nếu có pool PG. Lab/test thường không có → sổ ca im lặng bỏ qua."""
    pool = getattr(ctx, "admin_pool", None)
    if pool is None:
        return None
    try:
        from services.case_ledger.store import CaseLedgerStore

        return CaseLedgerStore(pool)
    except Exception as exc:  # noqa: BLE001
        logger.warning("advisory_ack: case ledger unavailable err=%s", exc)
        return None


async def open_advisory_case(
    ctx: Any,
    *,
    trace_id: str,
    tenant_id: str,
    lane: str = "",
    alertname: str = "",
) -> dict[str, Any]:
    """Mở ca LÚC advisory phát ra và trả về thông tin trí nhớ của pattern.

    Mở lúc phát biểu chứ không phải lúc có người bấm nút: nếu chỉ mở khi có phán quyết
    thì mẫu số chỉ gồm những ca có người quan tâm, và tỉ lệ im lặng — con số quan trọng
    nhất để chặn việc xin quyền — biến mất khỏi sổ.

    Best-effort tuyệt đối: mọi lỗi trả ``{}``, advisory vẫn phải được gửi đi.
    """
    store = _case_store(ctx)
    if store is None or not trace_id:
        return {}
    from services.learning_promoter.advisory_promoter import advisory_pattern_key

    pattern_key = advisory_pattern_key({"lane": lane, "alertname": alertname})
    if not pattern_key:
        return {}
    try:
        prior = await store.last_case_for_pattern(tenant_id=tenant_id, pattern_key=pattern_key)
        row = await store.open_case(
            case_id=trace_id,
            tenant_id=tenant_id,
            pattern_key=pattern_key,
            posture="DIAGNOSED",
            lane=lane,
            alertname=alertname,
        )
    except Exception as exc:  # noqa: BLE001 — sổ ca hỏng không được chặn đường phát advisory
        logger.warning("advisory_ack: open_case fail trace=%s err=%s", trace_id, exc)
        return {}

    memory: dict[str, Any] = {
        "pattern_key": pattern_key,
        "occurrence_no": int(row.get("occurrence_no") or 1) if row else 1,
    }
    if prior and str(prior.get("case_id") or "") != trace_id:
        memory.update({
            "prior_case_id": str(prior.get("case_id") or ""),
            "prior_opened_at": prior.get("opened_at"),
            "prior_diagnosis_verdict": prior.get("diagnosis_verdict"),
            "prior_remedy_verdict": prior.get("remedy_verdict"),
            "prior_recurred": bool(prior.get("recurred")),
        })
    return memory


async def _record_case_verdict(
    ctx: Any, *, trace_id: str, tenant_id: str, verdict: str, actor: str
) -> str | None:
    """Ghi phán quyết vào sổ ca, trả ``pattern_key`` ĐÃ ĐÓNG BĂNG lúc mở ca.

    Trả pattern của chính ca đó (không tính lại) để vòng học cộng điểm đúng nhóm mà sổ
    ca đã chốt — tính lại ở đây là một đường vòng để đổi nhóm sau khi biết kết quả.
    """
    store = _case_store(ctx)
    if store is None:
        return None
    try:
        row = await store.record_verdict(
            case_id=trace_id, source="telegram", actor=actor, diagnosis=verdict,
        )
    except Exception as exc:  # noqa: BLE001 — sổ ca hỏng không được chặn đường ack
        logger.warning("advisory_ack: case verdict fail trace=%s err=%s", trace_id, exc)
        return None
    _ = tenant_id  # ca đã mang tenant từ lúc mở; giữ tham số cho log/đối soát tương lai
    return str(row.get("pattern_key") or "") or None if row else None


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
    parsed = parse_advisory_verdict_callback(data)
    if parsed is None:
        return False
    trace_id, verdict = parsed

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
                "decision": verdict or ACKNOWLEDGED,
                "verdict": verdict,
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
                "verdict": verdict,
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

    # G2 — ack là mẫu KPI acceptance/false-positive. Không có nguồn nào khác điền
    # `omni:kpi:z:*` trong shadow mode, và đó chính là bằng chứng dùng để xét nâng
    # tier sau này. CORRECT và PARTIAL đều tính accepted (PARTIAL nói về độ đầy đủ
    # của khuyến nghị, không phải chẩn đoán sai — giữ nguyên ý định gốc).
    # #30: verdict=None (callback 1-nút cũ, tin nhắn đọng lại trong lịch sử chat
    # trước khi có 3 nút) KHÔNG mang phán quyết nào — trước đây điều kiện
    # `verdict != VERDICT_INCORRECT` coi None là "đồng ý", tái tạo đúng lớp bug
    # "đọc = đồng ý". Giờ None không ghi gì cả, tường minh.
    # #29: "Sai" (INCORRECT) trước đây chỉ bị *bỏ qua* — không ghi tín hiệu âm nào,
    # nên omni:kpi:z:*:false_positive mãi trống dù operator từ chối advisory nhiều
    # lần. Giờ verdict=INCORRECT ghi record_false_positive() thay vì im lặng.
    if redis is not None and verdict is not None:
        try:
            from workers.kpi_metrics import KPIStore

            store = KPIStore(redis)
            if verdict == VERDICT_INCORRECT:
                await store.record_false_positive(trace_id, tenant_id=tenant_id)
            else:  # CORRECT hoặc PARTIAL
                await store.record_accepted(trace_id, tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("advisory_ack: kpi record fail trace=%s err=%s", trace_id, exc)

    # Sổ ca là nguồn sự thật để đánh giá năng lực; phán quyết phải vào đó trước, và
    # pattern_key lấy ra từ đó là pattern đã đóng băng lúc mở ca.
    pattern_key: str | None = None
    if verdict is not None:
        pattern_key = await _record_case_verdict(
            ctx, trace_id=trace_id, tenant_id=tenant_id, verdict=verdict, actor=actor,
        )

    # G1 — vòng học: phán quyết của operator là tín hiệu học DUY NHẤT có thật trong shadow
    # mode (không mutation → không VERIFIED_SUCCESS → promoter.evaluate_for_promotion không
    # bao giờ chạy). Chỉ ĐÚNG/SAI mới là nhãn học: PARTIAL không thưởng cũng không phạt vì
    # "đúng nhưng thiếu" nói về độ đầy đủ của khuyến nghị, không nói chẩn đoán sai; còn
    # callback dạng cũ (verdict=None) không mang phán quyết nào để học.
    # Best-effort: học hỏng KHÔNG được làm hỏng việc ghi nhận advisory.
    if verdict in (VERDICT_CORRECT, VERDICT_INCORRECT):
        try:
            from services.learning_promoter.advisory_promoter import record_advisory_verdict

            await record_advisory_verdict(
                ctx,
                tenant_id=tenant_id,
                trace_id=trace_id,
                accepted=verdict == VERDICT_CORRECT,
                advisory=await _load_advisory_shape(redis, trace_id),
                pattern_key=pattern_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("advisory_ack: graduation skip trace=%s err=%s", trace_id, exc)

    if tg and cq_id:
        await tg.answer_callback_query(cq_id, text=_ANSWER_TEXT.get(verdict or "", "✅ Đã ghi nhận"))
    logger.info(
        "advisory_ack: judged trace=%s actor=%s verdict=%s", trace_id, actor, verdict or "-",
    )
    return True
