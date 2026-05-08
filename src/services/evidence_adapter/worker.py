"""AdapterGeneratorWorker — reads raw events from a Redis Stream, converts via EvidenceAdapter,
and emits Omni evidence envelopes to the omni-diagnostic-evidence Kafka topic.

This worker is the bridge between upstream SIEM / external event sources and Omni's analyst
pipeline. It is deployed as a separate K8s Deployment (omni-siem-bridge already covers the
alert path; this worker covers the direct evidence injection path).

Environment:
    ADAPTER_REDIS_URL         redis://...
    ADAPTER_STREAM            stream:siem_evidence_raw
    ADAPTER_GROUP             omni-evidence-adapters
    ADAPTER_CONSUMER          omni-evidence-adapter-1
    ADAPTER_BLOCK_MS          2000
    ADAPTER_BATCH             10
    OMNI_KAFKA_BOOTSTRAP_SERVERS  kafka:9092
    OMNI_KAFKA_TOPIC_EVIDENCE     omni-diagnostic-evidence
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from aiokafka import AIOKafkaProducer
from redis.asyncio import Redis

from .protocol import EvidenceAdapter

log = logging.getLogger(__name__)

# --- Config ---
_REDIS_URL = os.getenv("ADAPTER_REDIS_URL") or os.getenv("OMNI_REDIS_URL")
if not _REDIS_URL:
    raise RuntimeError("ADAPTER_REDIS_URL or OMNI_REDIS_URL must be set")
_REDIS_PASSWORD = os.getenv("ADAPTER_REDIS_PASSWORD", "")
_STREAM = os.getenv("ADAPTER_STREAM", "stream:siem_evidence_raw")
_GROUP = os.getenv("ADAPTER_GROUP", "omni-evidence-adapters")
_CONSUMER = os.getenv("ADAPTER_CONSUMER", "omni-evidence-adapter-1")
_BLOCK_MS = int(os.getenv("ADAPTER_BLOCK_MS", "2000"))
_BATCH = int(os.getenv("ADAPTER_BATCH", "10"))

_KAFKA_SERVERS = os.getenv("OMNI_KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
_KAFKA_TOPIC = os.getenv("OMNI_KAFKA_TOPIC_EVIDENCE", "omni-diagnostic-evidence")


class AdapterGeneratorWorker:
    """Reads raw events from a Redis Stream, adapts them, and publishes to Kafka."""

    def __init__(self, adapter: EvidenceAdapter) -> None:
        if not isinstance(adapter, EvidenceAdapter):
            raise TypeError(f"adapter must implement EvidenceAdapter protocol, got {type(adapter)}")
        self._adapter = adapter
        self._redis_backoff: int = 2

    async def run(self) -> None:
        redis = Redis.from_url(
            _REDIS_URL,
            password=_REDIS_PASSWORD or None,
            decode_responses=True,
        )
        producer = AIOKafkaProducer(
            bootstrap_servers=_KAFKA_SERVERS,
            value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode(),
        )
        # Retry Kafka bootstrap with exponential backoff (mirrors Redis bootstrap below).
        backoff = 2
        while True:
            try:
                await producer.start()
                break
            except Exception as e:
                log.warning("event=kafka_bootstrap_retry err=%s backoff_s=%d", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
        # Retry Redis ping with exponential backoff before entering the poll loop.
        # Cross-namespace DNS (redis.finguard-customer.svc) may be slow to resolve
        # on pod startup, causing repeated connection errors without this guard.
        redis_backoff = 2
        while True:
            try:
                await redis.ping()
                break
            except Exception as e:
                log.warning("event=redis_bootstrap_retry err=%s backoff_s=%d", e, redis_backoff)
                await asyncio.sleep(redis_backoff)
                redis_backoff = min(redis_backoff * 2, 30)
        try:
            await _ensure_group(redis)
            log.info(
                "event=adapter_worker_started stream=%s group=%s consumer=%s topic=%s",
                _STREAM, _GROUP, _CONSUMER, _KAFKA_TOPIC,
            )
            while True:
                await self._poll_once(redis, producer)
        finally:
            await producer.stop()
            await redis.aclose()

    async def _poll_once(self, redis: Redis, producer: AIOKafkaProducer) -> None:
        try:
            results = await redis.xreadgroup(
                groupname=_GROUP,
                consumername=_CONSUMER,
                streams={_STREAM: ">"},
                count=_BATCH,
                block=_BLOCK_MS,
            )
            self._redis_backoff = 2  # reset on success
        except Exception as e:
            log.warning("event=adapter_xreadgroup_error err=%s backoff_s=%d", e, self._redis_backoff)
            await asyncio.sleep(self._redis_backoff)
            self._redis_backoff = min(self._redis_backoff * 2, 30)
            return

        if not results:
            return

        for _stream_name, messages in results:
            for msg_id, fields in messages:
                await self._handle(redis, producer, msg_id, fields)

    async def _handle(
        self,
        redis: Redis,
        producer: AIOKafkaProducer,
        msg_id: str,
        fields: dict[str, Any],
    ) -> None:
        try:
            envelopes = self._adapter.to_evidence(fields)
            for env in envelopes:
                await producer.send_and_wait(
                    _KAFKA_TOPIC,
                    value={"data": json.dumps(env, ensure_ascii=False)},
                )
            await redis.xack(_STREAM, _GROUP, msg_id)
            log.info(
                "event=adapter_emitted msg_id=%s envelopes=%d trace_id=%s",
                msg_id,
                len(envelopes),
                envelopes[0].get("trace_id") if envelopes else "none",
            )
        except Exception as e:
            log.error("event=adapter_handle_error msg_id=%s err=%s", msg_id, e)


async def _ensure_group(redis: Redis) -> None:
    try:
        await redis.xgroup_create(_STREAM, _GROUP, id="$", mkstream=True)
    except Exception:
        pass  # group already exists


async def main() -> None:
    from .siem_adapter import SIEMEvidenceAdapter

    logging.basicConfig(
        level=logging.INFO,
        format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", "message": %(message)s}',
    )
    worker = AdapterGeneratorWorker(SIEMEvidenceAdapter())
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
