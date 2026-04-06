"""Kafka audit for agentic sessions (structured fields)."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _flatten_fields(data: dict[str, Any], maxlen: int) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in data.items():
        key = str(k)[:128]
        if isinstance(v, (dict, list)):
            s = json.dumps(v, ensure_ascii=False)[:maxlen]
        else:
            s = str(v)[:maxlen]
        out[key] = s
    return out


async def append_agent_audit(
    ctx: Any,
    *,
    phase: str,
    trace_id: str,
    event: str,
    **fields: Any,
) -> None:
    """Produce audit topic (default ``kafka_topic_audit_agent``); fields are stringified JSON-safe."""
    ws = getattr(ctx, "settings", None)
    if ws is None:
        return
    kafka = getattr(ctx, "kafka", None)
    if kafka is None:
        return
    maxlen = int(getattr(ws, "audit_agent_maxlen", 8000) or 8000)
    topic = getattr(ws, "kafka_topic_audit_agent", "") or ""
    if not topic.strip():
        return
    row: dict[str, Any] = {"phase": phase, "event": event, "trace_id": trace_id, **fields}
    try:
        flat = _flatten_fields(row, maxlen=maxlen)
        await kafka.send_dict(topic, flat)
    except Exception as e:
        logger.debug("[%s] append_agent_audit skip: %s", trace_id, e)
