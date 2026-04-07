from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fakeredis import FakeAsyncRedis

from workers.autonomy_contract import (
    TRANSITION_CONTEXT_READY,
    TRANSITION_DIAGNOSED,
    emit_terminal_tombstone,
    emit_transition,
)


class _KafkaCapture:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    async def send_dict(self, topic: str, envelope: dict) -> None:
        self.rows.append((topic, dict(envelope)))


@pytest.mark.asyncio
async def test_transition_sequence_and_terminal_tombstone() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    kafka = _KafkaCapture()
    ctx = SimpleNamespace(
        redis=redis,
        kafka=kafka,
        settings=SimpleNamespace(
            kafka_topic_audit_agent="omni-audit-agent",
            kafka_topic_dlq="omni-dlq",
        ),
    )
    trace = "it-trace-1"

    await emit_transition(
        ctx,
        trace_id=trace,
        transition=TRANSITION_CONTEXT_READY,
        component="integration_test",
        detail="ctx_ready",
    )
    await emit_transition(
        ctx,
        trace_id=trace,
        transition=TRANSITION_DIAGNOSED,
        component="integration_test",
        detail="diag_done",
    )
    await emit_terminal_tombstone(
        ctx,
        trace_id=trace,
        reason_code="TEST_ESCALATE",
        component="integration_test",
        detail="forced",
    )

    audit_payloads = [
        json.loads(row[1]["data"])
        for row in kafka.rows
        if row[0] == "omni-audit-agent" and isinstance(row[1].get("data"), str)
    ]
    assert [p.get("sequence") for p in audit_payloads[:2]] == [1, 2]
    assert any(p.get("transition") == "REQUIRES_HUMAN" for p in audit_payloads)

    terminal_raw = await redis.get(f"omni:autonomous:terminal:{trace}")
    assert terminal_raw is not None
    terminal = json.loads(terminal_raw)
    assert terminal.get("reason_code") == "TEST_ESCALATE"
