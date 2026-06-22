from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from workers.evidence_consumer import _derive_shadow_os_commands
from workers.kafka_actions_consumer import _handle_execute_mutate
from workers.schemas.agentic_planner import validate_suggest_os_runbook_data
from workers.autonomous_execute import run_execute_mutate_tool


def test_validate_suggest_os_runbook_requires_dry_run_and_rollback() -> None:
    payload = {
        "diagnosis": "x",
        "confidence": 0.8,
        "source": "shadow",
        "runbook_title": "t",
        "commands": [
            {
                "purpose": "precheck command health",
                "dry_run_command": "echo ok",
                "command": "echo run",
                "target": "node:a",
                "risk_level": "low",
                "expected_output": "ok",
                "rollback_command": "echo rollback",
                "timeout_sec": 30,
                "evidence_refs": ["E1"],
            }
        ],
    }
    out = validate_suggest_os_runbook_data(payload)
    assert out.commands[0].dry_run_command == "echo ok"
    assert out.commands[0].rollback_command == "echo rollback"


def test_derive_shadow_os_commands_contains_safety_pair() -> None:
    commands = _derive_shadow_os_commands(
        tool_name="k8s_rollout_restart",
        args={"namespace": "n", "deployment": "d"},
        evidence_refs=["E1"],
        trace="trace-1",
    )
    assert len(commands) >= 2
    for item in commands:
        assert item["dry_run_command"]
        assert item["rollback_command"]
        assert item["evidence_refs"]


@pytest.mark.asyncio
async def test_shadow_mode_blocks_sdk_mutate(monkeypatch: pytest.MonkeyPatch) -> None:
    feedback = AsyncMock()
    emit = AsyncMock()
    tomb = AsyncMock()
    monkeypatch.setattr("workers.kafka_actions_consumer.publish_action_feedback", feedback)
    monkeypatch.setattr("workers.kafka_actions_consumer.emit_transition", emit)
    monkeypatch.setattr("workers.kafka_actions_consumer.emit_terminal_tombstone", tomb)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(omni_shadow_os_mode=True, omni_auto_execute_enabled=True),
        redis=SimpleNamespace(),
    )
    await _handle_execute_mutate(
        ctx,
        trace="t-1",
        data={"tool_name": "k8s_rollout_restart", "args": {"namespace": "n", "deployment": "d"}},
    )
    assert feedback.await_count == 1
    assert tomb.await_count == 1


@pytest.mark.asyncio
async def test_executor_force_nsenter_blocks_non_kubectl_cluster() -> None:
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            omni_executor_force_nsenter=True,
            omni_unrestricted_tool_execution=False,
        ),
    )
    out, code = await run_execute_mutate_tool(
        ctx,
        tool_name="k8s_patch_resource",
        args={"namespace": "n"},
        trace_id="trace-1",
    )
    assert code == 1
    assert "must route through kubectl_cluster with nsenter host wrapper" in out
