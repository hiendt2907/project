"""Unit tests for services.playbook.state_machine.StepStateMachine."""
from __future__ import annotations

import pytest
import fakeredis.aioredis

from services.playbook.state_machine import (
    StepStateMachine,
    STEP_PENDING,
    STEP_APPROVED,
    STEP_EXECUTING,
    STEP_DONE,
    STEP_REJECTED,
    STEP_EXPIRED,
)


@pytest.fixture
async def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
async def sm(redis):
    return StepStateMachine(redis)


@pytest.mark.asyncio
async def test_init_creates_pending_state(sm):
    state = await sm.init("trace-1", "pb-1", step_order=0, tool_name="k8s_rollout_restart")
    assert state["status"] == STEP_PENDING
    assert state["playbook_id"] == "pb-1"
    assert state["trace_id"] == "trace-1"
    assert state["tool_name"] == "k8s_rollout_restart"
    assert state["step_order"] == 0
    assert state["updated_at"] > 0


@pytest.mark.asyncio
async def test_get_returns_persisted_state(sm):
    await sm.init("trace-1", "pb-1", step_order=0, tool_name="k8s_rollout_restart")
    state = await sm.get("trace-1", "pb-1")
    assert state is not None
    assert state["status"] == STEP_PENDING


@pytest.mark.asyncio
async def test_get_returns_none_for_missing_key(sm):
    assert await sm.get("nonexistent", "pb-999") is None


@pytest.mark.asyncio
async def test_valid_transition_pending_to_approved(sm):
    await sm.init("t", "pb", 0, "tool")
    ok = await sm.transition("t", "pb", STEP_APPROVED)
    assert ok is True
    state = await sm.get("t", "pb")
    assert state["status"] == STEP_APPROVED


@pytest.mark.asyncio
async def test_valid_transition_pending_to_rejected(sm):
    await sm.init("t", "pb", 0, "tool")
    ok = await sm.transition("t", "pb", STEP_REJECTED)
    assert ok is True
    state = await sm.get("t", "pb")
    assert state["status"] == STEP_REJECTED


@pytest.mark.asyncio
async def test_valid_transition_approved_to_executing(sm):
    await sm.init("t", "pb", 0, "tool")
    await sm.transition("t", "pb", STEP_APPROVED)
    ok = await sm.transition("t", "pb", STEP_EXECUTING)
    assert ok is True


@pytest.mark.asyncio
async def test_valid_transition_executing_to_done(sm):
    await sm.init("t", "pb", 0, "tool")
    await sm.transition("t", "pb", STEP_APPROVED)
    await sm.transition("t", "pb", STEP_EXECUTING)
    ok = await sm.transition("t", "pb", STEP_DONE)
    assert ok is True
    state = await sm.get("t", "pb")
    assert state["status"] == STEP_DONE


@pytest.mark.asyncio
async def test_invalid_transition_returns_false(sm):
    await sm.init("t", "pb", 0, "tool")
    ok = await sm.transition("t", "pb", STEP_EXECUTING)
    assert ok is False
    state = await sm.get("t", "pb")
    assert state["status"] == STEP_PENDING


@pytest.mark.asyncio
async def test_terminal_state_cannot_transition(sm):
    await sm.init("t", "pb", 0, "tool")
    await sm.transition("t", "pb", STEP_REJECTED)
    ok = await sm.transition("t", "pb", STEP_APPROVED)
    assert ok is False


@pytest.mark.asyncio
async def test_transition_on_missing_state_returns_false(sm):
    ok = await sm.transition("ghost", "pb", STEP_APPROVED)
    assert ok is False


@pytest.mark.asyncio
async def test_pending_can_expire(sm):
    await sm.init("t", "pb", 0, "tool")
    ok = await sm.transition("t", "pb", STEP_EXPIRED)
    assert ok is True
    state = await sm.get("t", "pb")
    assert state["status"] == STEP_EXPIRED


@pytest.mark.asyncio
async def test_init_updates_updated_at_on_transition(sm):
    state0 = await sm.init("t", "pb", 0, "tool")
    await sm.transition("t", "pb", STEP_APPROVED)
    state1 = await sm.get("t", "pb")
    assert state1["updated_at"] >= state0["updated_at"]
