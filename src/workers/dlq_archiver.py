"""DLQ Archiver — consume omni-dlq, archive to Redis, alert on repeated failures."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaConnectionError, KafkaError

from workers.handler_context import WorkerHandlerContext
from workers.metrics_exporter import inc_dlq_archived

logger = logging.getLogger(__name__)

# Redis ZSET: recent DLQ messages (score=unix_ts, member=payload_hash).
_REDIS_KEY = "omni:dlq:recent"
_REDIS_TTL_SEC = 7 * 24 * 3600  # 7 days
# Alert if the same trace_id accumulates this many DLQ entries.
_REPEAT_ALERT_THRESHOLD = 3

_TRANSIENT = (KafkaConnectionError, KafkaError, ConnectionError, OSError)


async def dlq_archiver_loop(ctx: WorkerHandlerContext, stop: asyncio.Event) -> None:
    """Consume omni-dlq, archive each message, Telegram-alert on repeated trace failures."""
    ws = ctx.settings
    bootstrap = ws.kafka_bootstrap_servers
    topic = ws.kafka_topic_dlq
    group_id = "omni-dlq-archiver"

    while not stop.is_set():
        consumer: AIOKafkaConsumer | None = None
        try:
            consumer = AIOKafkaConsumer(
                topic,
                bootstrap_servers=bootstrap,
                group_id=group_id,
                auto_offset_reset="earliest",
                enable_auto_commit=False,
                value_deserializer=lambda b: b,
            )
            await consumer.start()
            logger.info("event=dlq_archiver_started topic=%s group=%s", topic, group_id)

            async for msg in consumer:
                if stop.is_set():
                    break
                await _handle_dlq_message(ctx, msg.value)
                await consumer.commit()

        except _TRANSIENT as e:
            logger.warning("event=dlq_archiver_reconnect err=%s", e)
            await asyncio.sleep(5)
        except Exception as e:
            logger.exception("event=dlq_archiver_error err=%s", e)
            await asyncio.sleep(10)
        finally:
            if consumer is not None:
                try:
                    await consumer.stop()
                except Exception:
                    pass


async def _handle_dlq_message(ctx: WorkerHandlerContext, raw: bytes) -> None:
    try:
        payload: dict[str, Any] = json.loads(raw)
    except Exception:
        payload = {"raw": raw.decode("utf-8", errors="replace")}

    trace_id = str(payload.get("trace_id") or "unknown")
    origin_topic = str(payload.get("origin_topic") or payload.get("topic") or "unknown")
    error_reason = str(payload.get("error") or payload.get("reason") or "unknown")
    payload_hash = hashlib.sha256(raw).hexdigest()[:16]
    ts = time.time()

    logger.error(
        "event=dlq_message_archived trace_id=%s origin=%s reason=%s hash=%s",
        trace_id,
        origin_topic,
        error_reason,
        payload_hash,
    )

    inc_dlq_archived(origin_topic)

    try:
        pipe = ctx.redis.pipeline()
        pipe.zadd(_REDIS_KEY, {payload_hash: ts})
        pipe.expire(_REDIS_KEY, _REDIS_TTL_SEC)
        await pipe.execute()
    except Exception as e:
        logger.warning("event=dlq_redis_write_failed err=%s", e)

    await _maybe_alert_repeated_failure(ctx, trace_id, error_reason)


async def _maybe_alert_repeated_failure(
    ctx: WorkerHandlerContext,
    trace_id: str,
    error_reason: str,
) -> None:
    """Alert via Telegram if the same trace_id has failed more than _REPEAT_ALERT_THRESHOLD times."""
    if trace_id == "unknown":
        return

    count_key = f"omni:dlq:trace_count:{trace_id}"
    try:
        count = await ctx.redis.incr(count_key)
        await ctx.redis.expire(count_key, 3600)  # count window: 1 hour
    except Exception:
        return

    if count != _REPEAT_ALERT_THRESHOLD:
        return

    msg = (
        f"[DLQ] Repeated failure: trace_id={trace_id} ({count}x)\n"
        f"reason={error_reason[:200]}"
    )
    logger.error("event=dlq_repeated_failure trace_id=%s count=%d", trace_id, count)

    if ctx.telegram is None:
        return
    ws = ctx.settings
    chat_id = getattr(ws, "telegram_admin_chat_id", None)
    if chat_id is None:
        return
    try:
        await ctx.telegram.send_message(int(chat_id), msg[:4000])
    except Exception as e:
        logger.warning("event=dlq_telegram_alert_failed err=%s", e)
