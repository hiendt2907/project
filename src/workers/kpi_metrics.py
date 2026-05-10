"""KPI metrics collector — separate consumer group on omni-action-feedback.

Tracks MTTD, MTTR, advisory acceptance rate, and false positive rate.
Zero coupling with main pipeline: reads only from Kafka feedback topic.

Rolling window: ZADD with timestamp as score + ZREMRANGEBYSCORE to expire old entries.
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


class KPIStore:
    """Rolling window KPI store — ZADD with unix timestamp scores."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def _zadd_and_expire(self, key: str, member: str) -> None:
        now = time.time()
        cutoff = now - _WINDOW_SECONDS
        await self._redis.zadd(key, {member: now})
        await self._redis.zremrangebyscore(key, "-inf", cutoff)
        await self._redis.expire(key, int(_WINDOW_SECONDS * 2))

    async def record_accepted(self, trace_id: str) -> None:
        await self._zadd_and_expire("omni:kpi:z:accepted", trace_id)

    async def record_rejected(self, trace_id: str) -> None:
        await self._zadd_and_expire("omni:kpi:z:rejected", trace_id)

    async def record_false_positive(self, trace_id: str) -> None:
        await self._zadd_and_expire("omni:kpi:z:false_positive", trace_id)

    async def record_detected(self, trace_id: str, lane: str, ts: float) -> None:
        await self._redis.zadd(f"omni:kpi:detected:{lane}", {trace_id: ts})
        cutoff = ts - _WINDOW_SECONDS
        await self._redis.zremrangebyscore(f"omni:kpi:detected:{lane}", "-inf", cutoff)
        await self._redis.expire(f"omni:kpi:detected:{lane}", int(_WINDOW_SECONDS * 2))

    async def record_resolved(self, trace_id: str, lane: str, ts: float) -> None:
        await self._redis.zadd(f"omni:kpi:resolved:{lane}", {trace_id: ts})
        cutoff = ts - _WINDOW_SECONDS
        await self._redis.zremrangebyscore(f"omni:kpi:resolved:{lane}", "-inf", cutoff)
        await self._redis.expire(f"omni:kpi:resolved:{lane}", int(_WINDOW_SECONDS * 2))

    async def get_summary(self) -> dict:
        now = time.time()
        since = now - _WINDOW_SECONDS
        accepted = int(await self._redis.zcount("omni:kpi:z:accepted", since, "+inf") or 0)
        rejected = int(await self._redis.zcount("omni:kpi:z:rejected", since, "+inf") or 0)
        false_pos = int(await self._redis.zcount("omni:kpi:z:false_positive", since, "+inf") or 0)
        total_advisory = accepted + rejected
        total_executed = accepted
        return {
            "window_seconds": _WINDOW_SECONDS,
            "accepted": accepted,
            "rejected": rejected,
            "false_positive": false_pos,
            "acceptance_rate": round(accepted / total_advisory, 4) if total_advisory else None,
            "false_positive_rate": round(false_pos / total_executed, 4) if total_executed else None,
        }


async def _handle_feedback(store: KPIStore, fields: dict) -> None:
    import workers.metrics_exporter as me

    outcome = fields.get("outcome", "")
    trace_id = fields.get("trace_id", f"kpi-{time.time()}")
    lane = fields.get("lane", "unknown")
    detected_at = float(fields.get("detected_at", 0) or 0)
    resolved_at = float(fields.get("resolved_at", 0) or time.time())

    if outcome in ("success", "APPROVED", "verified"):
        await store.record_accepted(trace_id)
        me.inc_kpi_incident(lane, "accepted")
        if detected_at > 0:
            me.observe_kpi_mttr(lane, resolved_at - detected_at)
            await store.record_detected(trace_id, lane, detected_at)
            await store.record_resolved(trace_id, lane, resolved_at)
    elif outcome in ("rejected", "REJECTED"):
        await store.record_rejected(trace_id)
        me.inc_kpi_incident(lane, "rejected")
    elif outcome in ("fail", "executor_fail"):
        await store.record_false_positive(trace_id)
        me.inc_kpi_incident(lane, "false_positive")

    summary = await store.get_summary()
    if summary["acceptance_rate"] is not None:
        me.set_kpi_advisory_acceptance_rate(summary["acceptance_rate"])
    if summary["false_positive_rate"] is not None:
        me.set_kpi_false_positive_rate(summary["false_positive_rate"])


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
                            await _handle_feedback(store, fields)
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
