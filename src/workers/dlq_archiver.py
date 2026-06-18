"""DLQ Archiver — consume omni-dlq, archive to Redis, time-window ROLLUP alerting.

Gemini-sấy lesson (plan step 2): a per-trace Telegram alert on repeated failure causes
alert fatigue and — under a 10k-trace storm — ~thousands of messages that trip the bot's
rate limit and lock it out. Instead, failures are accumulated into **time-window buckets**
and a single rollup notification ("N traces failed, top reasons …") is emitted per window.

Atomicity (per user requirement): the storm hits the same bucket from thousands of
concurrent consumers in the same instant. A Python read-modify-write would race. The
accumulation is therefore a single Redis Lua script (one atomic round-trip: ZINCRBY the
reason histogram + INCR the bucket total + bump TTL). The flush loop claims each completed
bucket with SET NX so exactly one worker emits the rollup.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
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

# Rollup bucketing.
_DEFAULT_ROLLUP_WINDOW_SEC = 60
_ROLLUP_PREFIX = "omni:dlq:rollup"
_ROLLUP_TTL_SEC = 3600  # keep buckets 1h for late flush / inspection
_ROLLUP_TOP_REASONS = 5
_FLUSH_TICK_SEC = 10  # how often the flush loop wakes to drain completed buckets

_TRANSIENT = (KafkaConnectionError, KafkaError, ConnectionError, OSError)

# Atomic rollup accumulation. KEYS[1]=reason histogram zset, KEYS[2]=bucket total counter.
# ARGV[1]=reason, ARGV[2]=ttl. Returns the new bucket total. Single round-trip → race-free
# even when thousands of storm consumers hit the same bucket simultaneously.
_ROLLUP_ACCUM_LUA = """
redis.call('ZINCRBY', KEYS[1], 1, ARGV[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
local t = redis.call('INCR', KEYS[2])
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[2]))
return t
"""


def _rollup_window(ctx: WorkerHandlerContext) -> int:
    return int(getattr(ctx.settings, "dlq_rollup_window_sec", _DEFAULT_ROLLUP_WINDOW_SEC) or _DEFAULT_ROLLUP_WINDOW_SEC)


def _bucket_id(ts: float, window: int) -> int:
    return int(math.floor(ts / max(1, window)))


def _bucket_keys(bucket: int) -> tuple[str, str]:
    return f"{_ROLLUP_PREFIX}:{bucket}:reasons", f"{_ROLLUP_PREFIX}:{bucket}:total"


async def dlq_archiver_loop(ctx: WorkerHandlerContext, stop: asyncio.Event) -> None:
    """Consume omni-dlq, archive each message, accumulate rollup buckets (no per-trace spam)."""
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

    await _accumulate_rollup(ctx, error_reason, ts)


async def _accumulate_rollup(ctx: WorkerHandlerContext, error_reason: str, ts: float) -> None:
    """Atomically bump the current time-bucket's reason histogram + total (Lua, race-free)."""
    window = _rollup_window(ctx)
    bucket = _bucket_id(ts, window)
    reasons_key, total_key = _bucket_keys(bucket)
    # Normalise the reason to a bounded label so the histogram stays small under a storm
    # of distinct error strings (e.g. trailing trace ids / timestamps).
    reason_label = (error_reason or "unknown").strip().split("\n", 1)[0][:120] or "unknown"
    try:
        await ctx.redis.eval(
            _ROLLUP_ACCUM_LUA, 2, reasons_key, total_key, reason_label, str(_ROLLUP_TTL_SEC)
        )
    except Exception as e:
        logger.warning("event=dlq_rollup_accum_failed err=%s", e)


async def dlq_rollup_flush_loop(ctx: WorkerHandlerContext, stop: asyncio.Event) -> None:
    """Periodically flush COMPLETED rollup buckets → one Telegram notification per bucket."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_FLUSH_TICK_SEC)
            if stop.is_set():
                break
        except asyncio.TimeoutError:
            pass
        try:
            await flush_completed_buckets(ctx, now=time.time())
        except Exception as e:
            logger.warning("event=dlq_rollup_flush_error err=%s", e)


async def flush_completed_buckets(ctx: WorkerHandlerContext, *, now: float) -> int:
    """Flush every bucket strictly older than the current one. Returns # buckets flushed.

    Each bucket is claimed atomically via SET NX so exactly one worker emits its rollup,
    even with multiple archiver replicas. We scan a bounded recent range of buckets so a
    bucket that briefly missed its flush tick is still drained.
    """
    window = _rollup_window(ctx)
    current = _bucket_id(now, window)
    flushed = 0
    # Look back over the TTL horizon so late/missed buckets still flush exactly once.
    lookback = max(2, _ROLLUP_TTL_SEC // max(1, window))
    for bucket in range(current - 1, current - 1 - lookback, -1):
        if await _flush_bucket(ctx, bucket):
            flushed += 1
    return flushed


async def _flush_bucket(ctx: WorkerHandlerContext, bucket: int) -> bool:
    """Claim + flush a single bucket. Returns True if this caller emitted the rollup."""
    reasons_key, total_key = _bucket_keys(bucket)
    claim_key = f"{_ROLLUP_PREFIX}:{bucket}:flushed"
    try:
        total_raw = await ctx.redis.get(total_key)
    except Exception:
        return False
    if not total_raw:
        return False  # empty bucket, nothing to flush
    # Atomic single-winner claim — only the SET NX winner emits the notification.
    try:
        claimed = await ctx.redis.set(claim_key, "1", nx=True, ex=_ROLLUP_TTL_SEC)
    except Exception:
        return False
    if not claimed:
        return False

    total = int(total_raw)
    try:
        top = await ctx.redis.zrevrange(reasons_key, 0, _ROLLUP_TOP_REASONS - 1, withscores=True)
    except Exception:
        top = []

    await _emit_rollup_notification(ctx, bucket, total, top)
    # Buckets self-expire via TTL; reasons/total left for /metrics inspection.
    return True


def _format_top_reasons(top: list[Any]) -> str:
    lines = []
    for item in top:
        try:
            member, score = item
        except Exception:
            continue
        name = member.decode() if isinstance(member, bytes) else str(member)
        lines.append(f"  • {name} ×{int(score)}")
    return "\n".join(lines) if lines else "  • (no reason recorded)"


async def _emit_rollup_notification(
    ctx: WorkerHandlerContext, bucket: int, total: int, top: list[Any]
) -> None:
    window = _rollup_window(ctx)
    msg = (
        f"[DLQ Rollup] {total} message(s) failed trong {window}s.\n"
        f"Top nguyên nhân:\n{_format_top_reasons(top)}"
    )
    logger.error("event=dlq_rollup_flush bucket=%d total=%d", bucket, total)

    if ctx.telegram is None:
        return
    chat_id = getattr(ctx.settings, "telegram_admin_chat_id", None)
    if chat_id is None:
        return
    try:
        await ctx.telegram.send_message(int(chat_id), msg[:4000])
    except Exception as e:
        logger.warning("event=dlq_rollup_telegram_failed err=%s", e)
