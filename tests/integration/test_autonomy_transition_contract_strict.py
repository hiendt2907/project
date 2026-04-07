from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fakeredis import FakeAsyncRedis

from workers.autonomy_contract import (
    TRANSITION_CONTEXT_READY,
    TRANSITION_DIAGNOSED,
    TRANSITION_EXECUTED,
    TRANSITION_INGESTED,
    TRANSITION_PLAN_EMITTED,
    TRANSITION_VERIFIED_SUCCESS,
    emit_transition,
)


class _KafkaCapture:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    async def send_dict(self, topic: str, envelope: dict) -> None:
        self.rows.append((topic, dict(envelope)))


@pytest.mark.asyncio
async def test_strict_happy_path_transition_order_is_monotonic() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    kafka = _KafkaCapture()
    ctx = SimpleNamespace(
        redis=redis,
        kafka=kafka,
        settings=SimpleNamespace(kafka_topic_audit_agent="omni-audit-agent"),
    )
    trace = "strict-order-1"
    expected = [
        TRANSITION_INGESTED,
        TRANSITION_CONTEXT_READY,
        TRANSITION_DIAGNOSED,
        TRANSITION_PLAN_EMITTED,
        TRANSITION_EXECUTED,
        TRANSITION_VERIFIED_SUCCESS,
    ]
    for tr in expected:
        await emit_transition(ctx, trace_id=trace, transition=tr, component="strict_test")

    rows = [
        json.loads(r[1]["data"])
        for r in kafka.rows
        if r[0] == "omni-audit-agent" and isinstance(r[1].get("data"), str)
    ]
    assert [x["transition"] for x in rows] == expected
    seqs = [int(x.get("sequence") or 0) for x in rows]
    assert seqs == [1, 2, 3, 4, 5, 6]
    assert seqs == sorted(seqs)
