"""Kafka producer helpers — message envelope compatible with former Redis Stream field ``data``."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from aiokafka import AIOKafkaProducer

logger = logging.getLogger(__name__)

_TOPIC_OK = re.compile(r"^[a-zA-Z0-9._-]+$")


def is_valid_kafka_topic(name: str) -> bool:
    return bool(name and _TOPIC_OK.match(name.strip()))


async def create_producer(bootstrap_servers: str) -> AIOKafkaProducer:
    p = AIOKafkaProducer(
        bootstrap_servers=bootstrap_servers.strip(),
        enable_idempotence=True,
        acks="all",
    )
    await p.start()
    return p


def kafka_msg_id(topic: str, partition: int, offset: int) -> str:
    return f"kafka-{topic}-{partition}-{offset}"


def _trace_id_from_envelope_or_inner(body: dict[str, Any]) -> str:
    trace = str(body.get("trace_id") or "").strip()
    if trace:
        return trace
    data = body.get("data")
    if isinstance(data, str) and data.strip().startswith("{"):
        try:
            inner = json.loads(data)
            if isinstance(inner, dict):
                return str(inner.get("trace_id") or "").strip()
        except Exception:
            return ""
    return ""


def decode_kafka_value_to_fields(
    raw: bytes,
    headers: list[tuple[str, bytes]] | None = None,
) -> dict[str, str]:
    """JSON object → redis-stream-like string fields for existing handlers."""
    body: dict[str, Any] = json.loads(raw.decode("utf-8"))
    out: dict[str, str] = {}
    for k, v in body.items():
        key = str(k)[:128]
        if isinstance(v, (dict, list)):
            out[key] = json.dumps(v, ensure_ascii=False)
        elif v is None:
            out[key] = ""
        else:
            out[key] = str(v)
    if headers:
        for hk, hv in headers:
            if hk == "trace_id" and hv:
                try:
                    out["trace_id"] = hv.decode("utf-8", errors="replace")
                except Exception:
                    out["trace_id"] = str(hv)
                break
    return out


class KafkaBus:
    """Thin async wrapper around ``AIOKafkaProducer`` + topic names from settings."""

    def __init__(self, producer: AIOKafkaProducer) -> None:
        self._p = producer

    async def send_dict(self, topic: str, envelope: dict[str, Any]) -> None:
        if not is_valid_kafka_topic(topic):
            logger.warning("skip kafka send: invalid topic name %r", topic)
            return
        trace = _trace_id_from_envelope_or_inner(envelope)
        payload = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        headers: list[tuple[str, bytes]] = []
        if trace:
            headers.append(("trace_id", trace.encode("utf-8", errors="ignore")))
        await self._p.send_and_wait(topic, value=payload, headers=headers or None)

    async def send_envelope_inner(self, topic: str, inner: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
        env: dict[str, Any] = {"data": json.dumps(inner, ensure_ascii=False)}
        if extra:
            env.update(extra)
        await self.send_dict(topic, env)

    async def close(self) -> None:
        await self._p.stop()
