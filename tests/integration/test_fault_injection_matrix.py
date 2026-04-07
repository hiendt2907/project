from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis import FakeAsyncRedis

from workers.proactive_observer import _process_proactive_message


class _KafkaCapture:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    async def send_dict(self, topic: str, envelope: dict) -> None:
        self.rows.append((topic, dict(envelope)))


@pytest.mark.asyncio
async def test_fault_matrix_latency_timeout_sets_terminal_tombstone(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_pipeline(*_a: object, **_k: object) -> None:
        await asyncio.sleep(0.2)

    monkeypatch.setattr("workers.proactive_observer._proactive_event_pipeline", slow_pipeline)
    monkeypatch.setattr("workers.proactive_observer.proactive_kill_switch_engaged", AsyncMock(return_value=False))
    monkeypatch.setattr("workers.proactive_observer.observe_proactive_incident_duration", lambda *_a, **_k: None)
    monkeypatch.setattr("workers.proactive_observer._append_audit", AsyncMock())
    monkeypatch.setattr("workers.proactive_observer._append_dlq_proactive", AsyncMock(return_value="dlq-x"))

    redis = FakeAsyncRedis(decode_responses=True)
    kafka = _KafkaCapture()
    sem = SimpleNamespace(acquire_proactive=AsyncMock(return_value="tok"), release=AsyncMock())
    settings = SimpleNamespace(
        proactive_kill_switch_key="omni:proactive:kill_switch",
        proactive_gigo_require_cluster_identity=False,
        proactive_event_timeout_sec=0.05,
        kafka_topic_dlq="omni-dlq",
        kafka_topic_audit_agent="omni-audit-agent",
    )
    ctx = SimpleNamespace(redis=redis, kafka=kafka, settings=settings, semaphore=sem, inbound_proactive=False, inbound_trace_id="")

    raw = json.dumps(
        {
            "trace_id": "fault-trace-1",
            "rule_name": "R1",
            "canonical_query": "sum(up)",
            "namespace": "multi-agent",
            "trigger_promql": "sum(up)",
        }
    )
    await _process_proactive_message(ctx, "msg-1", raw)

    terminal_raw = await redis.get("omni:autonomous:terminal:fault-trace-1")
    assert terminal_raw is not None
    assert "EVENT_TIMEOUT" in terminal_raw


@pytest.mark.asyncio
async def test_fault_matrix_partial_failure_replan_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from workers.autonomous_feedback_loop import handle_action_feedback_envelope

    redis = FakeAsyncRedis(decode_responses=True)
    kafka = _KafkaCapture()
    settings = SimpleNamespace(
        autonomous_execute_max_attempts=3,
        autonomous_verify_max_rounds=3,
        kafka_topic_dlq="omni-dlq",
        kafka_topic_audit_agent="omni-audit-agent",
    )
    ctx = SimpleNamespace(redis=redis, kafka=kafka, settings=settings, ollama=SimpleNamespace(), ledger=SimpleNamespace(record_exception=AsyncMock()))
    monkeypatch.setattr("workers.autonomous_feedback_loop.emit_telegram_escalation", AsyncMock())
    monkeypatch.setattr("workers.autonomous_feedback_loop._llm_replan_after_feedback", AsyncMock(return_value=None))

    await handle_action_feedback_envelope(
        ctx,
        {
            "trace_id": "fault-trace-2",
            "data": json.dumps({"trace_id": "fault-trace-2", "exit_code": 2, "stdout": "", "stderr": "planner unavailable"}),
        },
    )
    terminal_raw = await redis.get("omni:autonomous:terminal:fault-trace-2")
    assert terminal_raw is not None
    assert "REPLAN_EMPTY" in terminal_raw
