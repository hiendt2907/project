"""Background loops cho autonomy tier — chạy ở role analyst/full (MASTER_PLAN §5/§7).

- ``crat_outbox_drainer_loop``: drain omni_admin.crat_outbox → CRAT block + publish
  metric omni_crat_outbox_pending.
- ``tier_readiness_loop``: định kỳ tính readiness từ KPI, ghi Redis
  ``omni:tier:readiness:{tenant}`` (gateway đọc) + metrics tier/promotion.

Cả hai no-op khi admin store offline (không pool) — fail-safe, không chặn worker.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_READINESS_KEY = "omni:tier:readiness:{tenant}"
_READINESS_INTERVAL_SEC = 300
_READINESS_TTL_SEC = 900


def _get_admin_pool(ctx: Any) -> Any | None:
    return getattr(ctx, "admin_pool", None)


async def crat_outbox_drainer_loop(ctx: Any, stop: asyncio.Event) -> None:
    """Drain CRAT outbox liên tục. Dừng khi ``stop`` set."""
    settings = ctx.settings
    if not bool(getattr(settings, "crat_outbox_drainer_enabled", True)):
        logger.info("crat_outbox drainer disabled by flag")
        return
    pool = _get_admin_pool(ctx)
    if pool is None:
        logger.info("crat_outbox drainer: admin pool offline — skip")
        return
    from services.admin_config import CratOutboxDrainer
    from workers.metrics_exporter import set_crat_outbox_pending

    drainer = CratOutboxDrainer(
        pool, redis=ctx.redis, kafka=ctx.kafka, settings=settings,
        kafka_topic=getattr(settings, "kafka_topic_audit_chain", "omni-audit-chain"),
    )
    poll = float(getattr(settings, "crat_outbox_poll_interval_sec", 5.0))
    while not stop.is_set():
        try:
            await drainer.drain_once()
            set_crat_outbox_pending(await drainer.pending_count())
        except Exception as exc:  # noqa: BLE001
            logger.error("crat_outbox drainer loop error: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll)
        except asyncio.TimeoutError:
            pass


async def tier_readiness_loop(ctx: Any, stop: asyncio.Event) -> None:
    """Tính & publish readiness định kỳ. CHỈ hiển thị — không set tier."""
    pool = _get_admin_pool(ctx)
    from workers.metrics_exporter import set_autonomy_tier, set_tier_promotion_ready
    from workers.tier_readiness import compute_tier_readiness

    repo = None
    if pool is not None:
        from services.admin_config import AdminConfigRepo

        repo = AdminConfigRepo(pool, redis=ctx.redis)

    tenant_id = "default"
    while not stop.is_set():
        try:
            tier = "shadow"
            entered_at = None
            if repo is not None:
                tier = await repo.get_tier(tenant_id) or "shadow"
                entered_at = await _tier_entered_at(pool, tenant_id)
            set_autonomy_tier(tier)
            readiness = await compute_tier_readiness(
                redis=ctx.redis, settings=ctx.settings, current_tier=tier,
                tenant_id=tenant_id, tier_entered_at=entered_at,
            )
            await ctx.redis.set(
                _READINESS_KEY.format(tenant=tenant_id),
                json.dumps(readiness.to_dict()),
                ex=_READINESS_TTL_SEC,
            )
            if readiness.next_tier:
                set_tier_promotion_ready(readiness.current_tier, readiness.next_tier, readiness.ready)
        except Exception as exc:  # noqa: BLE001
            logger.error("tier_readiness loop error: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_READINESS_INTERVAL_SEC)
        except asyncio.TimeoutError:
            pass


async def _tier_entered_at(pool: Any, tenant_id: str) -> float | None:
    """Epoch giây lúc vào tier hiện tại (từ updated_at)."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT extract(epoch FROM updated_at) AS ts "
                "FROM omni_admin.autonomy_tier_state WHERE tenant_id = $1",
                tenant_id,
            )
        return float(row["ts"]) if row and row["ts"] is not None else None
    except Exception:  # noqa: BLE001
        return None


_HITL_DECISIONS_TOPIC = "omni-hitl-decisions"


async def hitl_ui_decisions_loop(ctx: Any, stop: asyncio.Event) -> None:
    """Consume ``omni-hitl-decisions`` (Admin UI duyệt) → dispatch Kafka actions/feedback.

    Song song đường Telegram (handle_hitl_callback). CRAT HITL_DECISION đã do gateway
    enqueue outbox + drainer ghi → ở đây chỉ định tuyến. No-op nếu kafka chưa sẵn.
    """
    from aiokafka import AIOKafkaConsumer
    from aiokafka.errors import KafkaConnectionError, UnknownTopicOrPartitionError

    from workers.hitl_telegram import dispatch_hitl_ui_decision

    ws = ctx.settings
    group_id = getattr(ws, "consumer_group_hitl_ui", "omni-hitl-ui-decisions")
    transient = (KafkaConnectionError, UnknownTopicOrPartitionError, ConnectionError)
    backoff = 1
    while not stop.is_set():
        consumer = AIOKafkaConsumer(
            _HITL_DECISIONS_TOPIC,
            bootstrap_servers=ws.kafka_bootstrap_servers,
            group_id=group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        try:
            await consumer.start()
            backoff = 1
        except transient as exc:
            logger.warning("hitl_ui_decisions_loop connect_failed err=%s backoff=%d", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        try:
            async for msg in consumer:
                if stop.is_set():
                    break
                try:
                    decision_msg = json.loads(msg.value.decode())
                    await dispatch_hitl_ui_decision(ctx, decision_msg)
                    await consumer.commit()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("hitl_ui_decisions_loop message error: %s", exc)
                    await consumer.commit()  # poison-ack: không kẹt offset
        except transient as exc:
            logger.warning("hitl_ui_decisions_loop connection_lost err=%s reconnecting", exc)
        finally:
            await consumer.stop()
