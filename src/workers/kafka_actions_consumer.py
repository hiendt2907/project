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
from workers.log_preview import json_obj_preview, log_preview
from workers.request_trace import pop_trace_id, push_trace_id

logger = logging.getLogger(__name__)


def _omni_actions_body_preview(body: dict[str, Any]) -> str:
    """Human-readable English preview for executor audit logs."""
    act = str(body.get("action") or "").strip().lower()
    data = body.get("data")
    if act == "suggest_remediation" and isinstance(data, dict):
        diag = str(data.get("diagnosis") or "").replace("\n", " ").strip()[:900]
        tool = str(data.get("suggested_tool") or "").strip()
        conf = data.get("confidence")
        src = str(data.get("source") or "").strip()
        return (
            f"Diagnosis: {diag} "
            f"Confidence: {conf} Source: {src} Suggested tool: {tool}."
        )
    return json_obj_preview(body, max_chars=1200)


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
                action_raw = str(body.get("action") or "").strip()
                action = action_raw.lower()
                trace = str(body.get("trace_id") or kafka_msg_id(msg.topic, msg.partition, msg.offset))
                data = body.get("data")
                tok = push_trace_id(trace)
                try:
                    ctx.inbound_trace_id = trace
                    logger.info(
                        "[%s] event=omni_actions_in action=%s body_preview=%s",
                        trace,
                        action_raw or "(empty)",
                        _omni_actions_body_preview(body),
                    )
                    if not isinstance(data, dict):
                        logger.warning("[%s] omni-actions skip: data not object", trace)
                        await consumer.commit()
                        continue
                    if action == "execute_write_pending":
                        out = await execute_write_pending_from_redis(ctx, data)
                        logger.info(
                            "[%s] omni-actions execute_write_pending ok out_len=%s result_preview=%s",
                            trace,
                            len(out or ""),
                            log_preview(out, max_chars=1200),
                        )
                    elif action == "suggest_remediation":
                        pass
                    else:
                        logger.warning("[%s] omni-actions unknown action=%s", trace, action)
                    await consumer.commit()
                finally:
                    pop_trace_id(tok)
            except Exception as e:
                await ctx.ledger.record_exception(e, phase="4", component="kafka_actions_loop", swallow_errors=True)
                logger.exception("kafka_actions_loop message error: %s", e)
                await asyncio.sleep(0.5)
    finally:
        await consumer.stop()
