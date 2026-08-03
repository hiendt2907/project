"""KPI metrics collector — separate consumer group on omni-action-feedback.

Tracks MTTD, MTTR, advisory acceptance rate, and false positive rate.
Zero coupling with main pipeline: reads only from Kafka feedback topic.

Rolling window: ZADD with timestamp as score + ZREMRANGEBYSCORE to expire old entries.
Per-tenant keys: omni:kpi:z:{tenant_id}:accepted|rejected|false_positive
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_CONSUMER_GROUP = "omni-kpi-collector"
_FEEDBACK_TOPIC = "omni-action-feedback"
_WINDOW_SECONDS = 86400  # 24h rolling window


OUTCOME_ACCEPTED = "accepted"
OUTCOME_REJECTED = "rejected"
OUTCOME_FALSE_POSITIVE = "false_positive"


def kpi_domain_from_lane_field(value: str | None) -> str:
    """Chuẩn hoá trường ``lane`` của message feedback thành một domain canonical.

    Trường này KHÔNG có một từ vựng duy nhất: tuỳ nơi phát, nó mang lane trục A
    (``SYS_RESOURCE``) hoặc proof_lane trục B (``resource``, ``state``, ``siem``) —
    xem cảnh báo ba trục trong ``pkg/domain/taxonomy.py``. Nhóm KPI theo domain nên
    phải nhận cả hai thay vì đoán một trục rồi ném phần còn lại vào ``unknown``.

    Thứ tự thử có chủ đích: trục A trước (khớp chính xác, và ``sys_hard_fail`` ở đó
    cố ý trả ``unknown``), rồi mới tới alias domain.
    """
    from pkg.domain.taxonomy import UNKNOWN, lane_to_domain, normalize_domain

    raw = (value or "").strip()
    if not raw:
        return UNKNOWN
    d = lane_to_domain(raw)
    if d != UNKNOWN:
        return d
    return normalize_domain(raw)


def kpi_outcome_key(tenant_id: str, outcome: str) -> str:
    """Nguồn DUY NHẤT dựng key outcome — writer và reader bắt buộc dùng chung.

    Trước đây `KPIStore` ghi `omni:kpi:z:{tenant}:accepted` còn
    `promoter._get_fp_rate`/`pkg.autonomy.gate` (xoá ở WS2, code chết) đọc
    `omni:kpi:z:accepted`; Redis lab tồn tại song song cả 2 dạng. Mọi phép đọc
    FP-rate vì thế luôn thấy 0 mẫu và gate chất lượng im lặng cho qua. Đừng nối
    chuỗi key thủ công ở bất kỳ đâu khác.
    """
    return f"omni:kpi:z:{tenant_id}:{outcome}"


async def read_outcome_rates(
    redis: Any, *, tenant_id: str = "default", window_seconds: int = _WINDOW_SECONDS
) -> dict[str, Any]:
    """Đọc outcome trong cửa sổ trượt.

    ``fp_rate``/``acceptance_rate`` là ``None`` khi CHƯA CÓ dữ liệu — cố ý không trả
    0.0, vì "0% false positive" và "chưa biết gì" phải được caller xử lý khác nhau
    (fail-closed), không được lẫn lộn.
    """
    since = time.time() - window_seconds
    counts: dict[str, int] = {}
    for outcome in (OUTCOME_ACCEPTED, OUTCOME_REJECTED, OUTCOME_FALSE_POSITIVE):
        try:
            counts[outcome] = int(
                await redis.zcount(kpi_outcome_key(tenant_id, outcome), since, "+inf") or 0
            )
        except Exception:  # noqa: BLE001 — thiếu key/redis lỗi = coi như 0 mẫu
            counts[outcome] = 0

    total = sum(counts.values())
    return {
        **counts,
        "total": total,
        "acceptance_rate": (counts[OUTCOME_ACCEPTED] / total) if total else None,
        "fp_rate": (counts[OUTCOME_FALSE_POSITIVE] / total) if total else None,
    }


class KPIStore:
    """Rolling window KPI store — ZADD with unix timestamp scores, per-tenant keys."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def _zadd_and_expire(self, key: str, member: str) -> None:
        now = time.time()
        cutoff = now - _WINDOW_SECONDS
        await self._redis.zadd(key, {member: now})
        await self._redis.zremrangebyscore(key, "-inf", cutoff)
        await self._redis.expire(key, int(_WINDOW_SECONDS * 2))

    async def record_accepted(self, trace_id: str, tenant_id: str = "default") -> None:
        await self._zadd_and_expire(kpi_outcome_key(tenant_id, OUTCOME_ACCEPTED), trace_id)

    async def record_rejected(self, trace_id: str, tenant_id: str = "default") -> None:
        await self._zadd_and_expire(kpi_outcome_key(tenant_id, OUTCOME_REJECTED), trace_id)

    async def record_false_positive(self, trace_id: str, tenant_id: str = "default") -> None:
        await self._zadd_and_expire(
            kpi_outcome_key(tenant_id, OUTCOME_FALSE_POSITIVE), trace_id
        )

    async def record_detected(self, trace_id: str, lane: str, ts: float, tenant_id: str = "default") -> None:
        """Ghi mốc phát hiện. ``lane`` được chuẩn hoá thành domain trước khi làm khoá.

        Chuẩn hoá NGAY TẠI ĐÂY (không ở caller) để mọi đường gọi cũ — kể cả đường
        truyền lane trục A — cùng đổ vào một khoá duy nhất. Nếu chuẩn hoá ở caller,
        một caller bị bỏ sót sẽ âm thầm tạo khoá song song và làm KPI hụt số.
        """
        key = f"omni:kpi:detected:{tenant_id}:{kpi_domain_from_lane_field(lane)}"
        await self._redis.zadd(key, {trace_id: ts})
        cutoff = ts - _WINDOW_SECONDS
        await self._redis.zremrangebyscore(key, "-inf", cutoff)
        await self._redis.expire(key, int(_WINDOW_SECONDS * 2))

    async def record_resolved(self, trace_id: str, lane: str, ts: float, tenant_id: str = "default") -> None:
        """Ghi mốc khắc phục. Xem ``record_detected`` về lý do chuẩn hoá tại đây."""
        key = f"omni:kpi:resolved:{tenant_id}:{kpi_domain_from_lane_field(lane)}"
        await self._redis.zadd(key, {trace_id: ts})
        cutoff = ts - _WINDOW_SECONDS
        await self._redis.zremrangebyscore(key, "-inf", cutoff)
        await self._redis.expire(key, int(_WINDOW_SECONDS * 2))

    async def get_summary(self, tenant_id: str | None = None) -> dict:
        """Tổng hợp outcome trong cửa sổ trượt.

        ``tenant_id=None`` giữ hành vi cũ (gộp mọi tenant) cho các caller chỉ cần một
        con số tổng. Đường Prometheus PHẢI truyền ``tenant_id``: một tỉ lệ toàn cục làm
        dữ liệu khách A lái cảnh báo của khách B, ngược với INV_NAMESPACE_ISOLATION và
        ngược với chính ``read_outcome_rates`` vốn per-tenant.

        ``acceptance_rate``/``false_positive_rate`` là ``None`` khi mẫu số bằng 0 —
        cùng bất biến với ``read_outcome_rates``: "chưa biết" KHÔNG được rơi xuống 0.0.
        Caller nào xuất ra Gauge phải xuất kèm ``total_advisory`` để phía đọc phân biệt
        được hai trạng thái đó (Gauge không mang được ``None``).
        """
        since = time.time() - _WINDOW_SECONDS

        async def _count(outcome: str) -> int:
            if tenant_id is not None:
                key = kpi_outcome_key(tenant_id, outcome)
                return int(await self._redis.zcount(key, since, "+inf") or 0)
            n = 0
            async for key in self._redis.scan_iter(f"omni:kpi:z:*:{outcome}", count=100):
                n += int(await self._redis.zcount(key, since, "+inf") or 0)
            return n

        accepted = await _count(OUTCOME_ACCEPTED)
        rejected = await _count(OUTCOME_REJECTED)
        false_pos = await _count(OUTCOME_FALSE_POSITIVE)
        total_advisory = accepted + rejected
        total_executed = accepted
        return {
            "window_seconds": _WINDOW_SECONDS,
            "tenant_id": tenant_id,
            "accepted": accepted,
            "rejected": rejected,
            "false_positive": false_pos,
            "total_advisory": total_advisory,
            "acceptance_rate": round(accepted / total_advisory, 4) if total_advisory else None,
            "false_positive_rate": round(false_pos / total_executed, 4) if total_executed else None,
        }


async def _handle_feedback(store: KPIStore, fields: dict, redis: Any = None) -> None:
    import workers.metrics_exporter as me

    outcome = fields.get("outcome", "")
    trace_id = fields.get("trace_id", f"kpi-{time.time()}")
    lane = fields.get("lane", "unknown")
    tenant_id = fields.get("tenant_id", "default")
    resolved_at = float(fields.get("resolved_at", 0) or time.time())

    # Use early-registered detection timestamp when available (registered by evidence_consumer
    # at batch receipt time — more accurate MTTD than feedback loop timestamp).
    detected_at = float(fields.get("detected_at", 0) or 0)
    if redis is not None and trace_id:
        try:
            early_ts = await redis.get(f"omni:incident:ts:{trace_id}")
            if early_ts:
                detected_at = float(early_ts)
        except Exception:
            pass  # fallback to feedback-provided detected_at

    if outcome in ("success", "APPROVED", "verified"):
        await store.record_accepted(trace_id, tenant_id)
        me.inc_kpi_incident(lane, "accepted")
        if detected_at > 0:
            me.observe_kpi_mttr(lane, resolved_at - detected_at)
            await store.record_detected(trace_id, lane, detected_at, tenant_id)
            await store.record_resolved(trace_id, lane, resolved_at, tenant_id)
    elif outcome in ("rejected", "REJECTED"):
        await store.record_rejected(trace_id, tenant_id)
        me.inc_kpi_incident(lane, "rejected")
    elif outcome in ("fail", "executor_fail"):
        await store.record_false_positive(trace_id, tenant_id)
        me.inc_kpi_incident(lane, "false_positive")

    # Per-tenant: gauge toàn cục cũ trộn mọi khách vào một tỉ lệ.
    summary = await store.get_summary(tenant_id=tenant_id)
    # Mẫu số xuất TRƯỚC và LUÔN xuất — kể cả bằng 0. Đây là thứ duy nhất cho phía đọc
    # phân biệt "0% chấp nhận" với "chưa ai phán lần nào"; alert rule bám vào nó.
    me.set_kpi_advisory_total(summary["total_advisory"], tenant=tenant_id)
    if summary["acceptance_rate"] is not None:
        me.set_kpi_advisory_acceptance_rate(summary["acceptance_rate"], tenant=tenant_id)
    if summary["false_positive_rate"] is not None:
        me.set_kpi_false_positive_rate(summary["false_positive_rate"], tenant=tenant_id)


async def run_kpi_collector(
    *,
    redis: Any,
    kafka_bootstrap: str,
    stop: asyncio.Event,
) -> None:
    from aiokafka import AIOKafkaConsumer

    store = KPIStore(redis)
    consumer = AIOKafkaConsumer(
        _FEEDBACK_TOPIC,
        bootstrap_servers=kafka_bootstrap,
        group_id=_CONSUMER_GROUP,
        auto_offset_reset="earliest",
        enable_auto_commit=True,
        value_deserializer=lambda v: json.loads(v.decode("utf-8", errors="replace")),
    )
    try:
        await consumer.start()
        logger.info("kpi_collector started group=%s topic=%s", _CONSUMER_GROUP, _FEEDBACK_TOPIC)
        while not stop.is_set():
            try:
                records = await asyncio.wait_for(
                    consumer.getmany(timeout_ms=2000, max_records=50),
                    timeout=5.0,
                )
                for _tp, messages in records.items():
                    for msg in messages:
                        try:
                            fields = msg.value if isinstance(msg.value, dict) else {}
                            await _handle_feedback(store, fields, redis=redis)
                        except Exception as e:
                            logger.debug("kpi_collector handle error: %s", e)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("kpi_collector loop error: %s", e)
                await asyncio.sleep(5)
    finally:
        await consumer.stop()
        logger.info("kpi_collector stopped")
