"""Consume ``omni-actions`` — **only** ``pkg.executor`` mutation paths (separate from analyst)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiokafka import AIOKafkaConsumer

from messaging.kafka_bus import decode_kafka_value_to_fields, kafka_msg_id
from pkg.executor import execute_write_pending_from_redis
from workers.handler_context import WorkerHandlerContext
from workers.request_trace import pop_trace_id, push_trace_id

logger = logging.getLogger(__name__)


async def kafka_actions_loop(ctx: WorkerHandlerContext, stop: asyncio.Event) -> None:
    """Executor role: ``action`` + ``data`` envelope → ``execute_write_pending_from_redis`` (rollout/write kinds)."""
    ws = ctx.settings
    consumer = AIOKafkaConsumer(
        ws.kafka_topic_actions,
        bootstrap_servers=ws.kafka_bootstrap_servers,
        group_id=ws.consumer_group_executor,
        enable_auto_commit=False,
        client_id=ws.consumer_name_executor,
    )
    await consumer.start()
    try:
        async for msg in consumer:
            if stop.is_set():
                break
            try:
                fields = decode_kafka_value_to_fields(msg.value)
                raw = fields.get("data") or fields.get("payload") or "{}"
                body = json.loads(raw)
                action = str(body.get("action") or "").strip().lower()
                trace = str(body.get("trace_id") or kafka_msg_id(msg.topic, msg.partition, msg.offset))
                data = body.get("data")
                if not isinstance(data, dict):
                    logger.warning("[%s] omni-actions skip: data not object", trace)
                    await consumer.commit()
                    continue
                tok = push_trace_id(trace)
                try:
                    ctx.inbound_trace_id = trace
                    if action == "execute_write_pending":
                        out = await execute_write_pending_from_redis(ctx, data)
                        logger.info("[%s] omni-actions execute_write_pending ok out_len=%s", trace, len(out or ""))
                    elif action == "ping":
                        logger.info("[%s] omni-actions ping", trace)
                    else:
                        logger.warning("[%s] omni-actions unknown action=%s", trace, action)
                finally:
                    pop_trace_id(tok)
                await consumer.commit()
            except Exception as e:
                await ctx.ledger.record_exception(e, phase="4", component="kafka_actions_loop", swallow_errors=True)
                logger.exception("kafka_actions_loop message error: %s", e)
                await asyncio.sleep(0.5)
    finally:
        await consumer.stop()
