"""TDD: meta_self self-monitoring/KPI alerts (OmniAdvisoryAcceptanceRateLow, ...) must not
burn a RAG+LLM cycle and escalate to DLQ on every re-fire of the same underlying KPI alert.

Bug: classify_alert() already flags these mutate_eligible=False (alert_envelope.py), but
evidence_consumer still ran the full RAG/LLM diagnosis -> SDK_ESCALATE -> emit_terminal_tombstone
-> DLQ for every occurrence, every ~5min (proactive eval interval), flooding the DLQ archive
and spamming Telegram with non-actionable "investigate further" hallucinated text.

Fix: _handle_meta_self_alert short-circuits right after evidence batch is ready, before
RAG/LLM/contrast, dispatches one deterministic advisory, and dedups subsequent re-fires of
the same alertname within a cooldown window — never touching DLQ for this class.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
import pytest

os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OMNI_OLLAMA_BASE_URL", "http://localhost:11434")

from workers.evidence_consumer import _handle_meta_self_alert


class _KafkaCapture:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_dict(self, topic: str, payload: dict, **kwargs: Any) -> None:
        self.sent.append((topic, payload))


def _make_ctx(**kw: Any) -> SimpleNamespace:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    kafka = _KafkaCapture()
    settings = SimpleNamespace(
        trace_correlation_ping_enabled=True,
        kafka_topic_actions="omni-actions",
        kafka_topic_audit_chain="omni-audit-chain",
        omni_shadow_os_mode=False,
        meta_self_alert_cooldown_sec=1800,
    )
    return SimpleNamespace(settings=settings, redis=redis, kafka=kafka, telegram=None, **kw)


async def _set_alert_class(ctx: SimpleNamespace, trace: str, *, alertname: str) -> None:
    await ctx.redis.setex(
        f"omni:trace:{trace}:alert_class",
        3600,
        json.dumps({"kind": "meta_self", "mutate_eligible": False,
                    "missing_fields": ["self_monitoring_alert_no_cluster_target"],
                    "alertname": alertname}),
    )


@pytest.mark.asyncio
async def test_non_meta_self_trace_falls_through() -> None:
    ctx = _make_ctx()
    out = await _handle_meta_self_alert(ctx, trace="gw-prom-noclass")
    assert out is None
    assert ctx.kafka.sent == []


@pytest.mark.asyncio
async def test_meta_self_first_fire_dispatches_deterministic_advisory_no_llm() -> None:
    ctx = _make_ctx()
    trace = "gw-prom-aaa111"
    await _set_alert_class(ctx, trace, alertname="OmniAdvisoryAcceptanceRateLow")

    out = await _handle_meta_self_alert(ctx, trace=trace)

    assert out is not None
    assert "META_SELF" in out
    topics = [t for t, _ in ctx.kafka.sent]
    assert "omni-actions" in topics
    assert "omni-audit-chain" in topics
    action_payload = next(p for t, p in ctx.kafka.sent if t == "omni-actions")
    body = json.loads(action_payload["data"])["data"]
    assert body["source"] == "META_SELF_DETERMINISTIC"
    assert "OmniAdvisoryAcceptanceRateLow" in body["diagnosis"]


@pytest.mark.asyncio
async def test_meta_self_repeat_within_cooldown_is_deduped_not_redispatched() -> None:
    ctx = _make_ctx()
    trace1 = "gw-prom-bbb111"
    trace2 = "gw-prom-bbb222"
    await _set_alert_class(ctx, trace1, alertname="OmniAdvisoryAcceptanceRateLow")
    await _set_alert_class(ctx, trace2, alertname="OmniAdvisoryAcceptanceRateLow")

    first = await _handle_meta_self_alert(ctx, trace=trace1)
    sent_after_first = len(ctx.kafka.sent)
    second = await _handle_meta_self_alert(ctx, trace=trace2)

    assert "DEDUPED" not in first
    assert "DEDUPED" in second
    # Re-fire must not enqueue another advisory dispatch or audit block.
    assert len(ctx.kafka.sent) == sent_after_first


@pytest.mark.asyncio
async def test_meta_self_different_alertnames_each_get_own_cooldown() -> None:
    ctx = _make_ctx()
    t1, t2 = "gw-prom-ccc111", "gw-prom-ccc222"
    await _set_alert_class(ctx, t1, alertname="OmniAdvisoryAcceptanceRateLow")
    await _set_alert_class(ctx, t2, alertname="OmniWorkerStalled")

    out1 = await _handle_meta_self_alert(ctx, trace=t1)
    out2 = await _handle_meta_self_alert(ctx, trace=t2)

    assert "DEDUPED" not in out1
    assert "DEDUPED" not in out2
