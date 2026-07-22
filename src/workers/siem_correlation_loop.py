"""kafka_siem_correlation_loop — Python port của brain-go (app.runKafka).

Consume ``omni-siem-raw`` → (1) passthrough incident envelope sang
``omni-siem-incidents``; (2) graph-correlate, emit CorrelationChain sang
``omni-siem-chains``. Gate bằng ``settings.siem_correlation_enabled``.

Error semantics mirror Go: decode lỗi → drop+ack; produce/correlate lỗi →
log (+ ledger) và tiếp tục consume — cả hai nhánh đều không chặn loop, đúng
hành vi engine Go đang deploy. Async-only (aiokafka + redis.asyncio).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from services.siem_correlation import config as corr_config
from services.siem_correlation.decode import decode_kafka_message, incident_envelope
from services.siem_correlation.graph import GraphConfig, GraphCorrelator

logger = logging.getLogger(__name__)


async def _handle_raw_message(ctx: Any, correlator: Any, msg: Any) -> None:
    """Process one ``omni-siem-raw`` message (decode → passthrough → correlate)."""
    inc = decode_kafka_message(msg.value)
    if inc is None:
        logger.warning(
            "event=siem_corr_drop_invalid partition=%s offset=%s", msg.partition, msg.offset
        )
        return

    try:
        await ctx.kafka.send_dict(
            corr_config.topic_siem_incidents(),
            incident_envelope(inc),
            key=inc.incident_id.encode(),
        )
    except Exception as e:  # noqa: BLE001 — parity: Go logs produce errors and continues
        logger.exception("event=siem_corr_incident_produce_failed incident=%s", inc.incident_id)
        await ctx.ledger.record_exception(
            e, phase="4", component="kafka_siem_correlation_loop", swallow_errors=True
        )

    try:
        chain = await correlator.process(inc)
        if chain is not None:
            await ctx.kafka.send_dict(
                corr_config.topic_siem_chains(),
                chain,
                key=str(chain.get("chain_id") or "").encode(),
            )
            logger.info(
                "event=siem_corr_chain_emitted chain_id=%s tenant=%s category=%s "
                "confidence=%s members=%d",
                chain.get("chain_id"),
                chain.get("tenant_id"),
                chain.get("attack_category"),
                chain.get("confidence"),
                len(chain.get("member_events") or []),
            )
    except Exception as e:  # noqa: BLE001 — parity: Go warns on correlation errors and continues
        logger.exception("event=siem_corr_chain_failed incident=%s", inc.incident_id)
        await ctx.ledger.record_exception(
            e, phase="4", component="kafka_siem_correlation_loop", swallow_errors=True
        )


async def kafka_siem_correlation_loop(ctx: Any, stop: asyncio.Event) -> None:
    """Consumer loop — same connect/backoff/commit skeleton as sibling loops
    (kafka_siem_chains_loop)."""
    from aiokafka import AIOKafkaConsumer
    from aiokafka.errors import KafkaConnectionError, UnknownTopicOrPartitionError

    ws = ctx.settings
    await ctx.scout_ready.wait()
    transient_errors = (KafkaConnectionError, UnknownTopicOrPartitionError, ConnectionError)
    connect_backoff = 1
    correlator = GraphCorrelator(ctx.redis, GraphConfig.from_env())

    while not stop.is_set():
        consumer = AIOKafkaConsumer(
            corr_config.topic_siem_raw(),
            bootstrap_servers=ws.kafka_bootstrap_servers,
            group_id=corr_config.consumer_group(),
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        try:
            await consumer.start()
            connect_backoff = 1
        except transient_errors as e:
            logger.warning(
                "kafka_siem_correlation_loop connect_failed err=%s backoff_s=%d",
                e, connect_backoff,
            )
            await asyncio.sleep(connect_backoff)
            connect_backoff = min(connect_backoff * 2, 30)
            continue
        try:
            async for msg in consumer:
                if stop.is_set():
                    break
                await _handle_raw_message(ctx, correlator, msg)
                await consumer.commit()
        except transient_errors as e:
            logger.warning("kafka_siem_correlation_loop connection_lost err=%s reconnecting", e)
        finally:
            await consumer.stop()
