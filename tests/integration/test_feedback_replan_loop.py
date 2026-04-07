from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis import FakeAsyncRedis

from workers.autonomous_feedback_loop import handle_action_feedback_envelope


class _KafkaCapture:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    async def send_dict(self, topic: str, envelope: dict) -> None:
        self.rows.append((topic, dict(envelope)))


@pytest.mark.asyncio
async def test_feedback_replan_empty_emits_terminal_tombstone(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    kafka = _KafkaCapture()
    settings = SimpleNamespace(
        autonomous_execute_max_attempts=3,
        autonomous_verify_max_rounds=3,
        kafka_topic_dlq="omni-dlq",
        kafka_topic_audit_agent="omni-audit-agent",
    )
    ctx = SimpleNamespace(
        redis=redis,
        kafka=kafka,
        settings=settings,
        ollama=SimpleNamespace(),
        ledger=SimpleNamespace(record_exception=AsyncMock()),
    )
    monkeypatch.setattr(
        "workers.autonomous_feedback_loop.emit_telegram_escalation",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "workers.autonomous_feedback_loop._llm_replan_after_feedback",
        AsyncMock(return_value=None),
    )

    fields = {
        "trace_id": "fb-trace-1",
        "data": json.dumps(
            {
                "trace_id": "fb-trace-1",
                "exit_code": 1,
                "stdout": "tool failed",
                "stderr": "boom",
            }
        ),
    }
    await handle_action_feedback_envelope(ctx, fields)

    terminal_raw = await redis.get("omni:autonomous:terminal:fb-trace-1")
    assert terminal_raw is not None
    terminal = json.loads(terminal_raw)
    assert terminal.get("reason_code") == "REPLAN_EMPTY"
    assert any(topic == "omni-dlq" for topic, _ in kafka.rows)
