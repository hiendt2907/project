"""Coverage tests for services.playbook.state_machine.

Uses fakeredis.aioredis as the Redis backend — no unittest.mock.
"""
from __future__ import annotations

import json

import fakeredis.aioredis
import pytest

from services.playbook.state_machine import (
    STEP_APPROVED,
    STEP_DONE,
    STEP_EXECUTING,
    STEP_EXPIRED,
    STEP_PENDING,
    STEP_REJECTED,
    StepStateMachine,
    _KEY_PATTERN,
    _TTL_SEC,
)


@pytest.fixture
def fake_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


# ── _key / get on missing entry ───────────────────────────────────────────────

def test_key_builder_pattern(fake_redis):
    sm = StepStateMachine(fake_redis)
    assert sm._key("trace-1", "pb-7") == _KEY_PATTERN.format(trace="trace-1", playbook_id="pb-7")


@pytest.mark.asyncio
async def test_get_returns_none_when_absent(fake_redis):
    sm = StepStateMachine(fake_redis)
    assert await sm.get("missing-trace", "missing-pb") is None


@pytest.mark.asyncio
async def test_get_returns_parsed_dict(fake_redis):
    sm = StepStateMachine(fake_redis)
    await sm.init("trace-a", "pb-a", step_order=1, tool_name="tool_x")
    state = await sm.get("trace-a", "pb-a")
    assert state is not None
    assert state["status"] == STEP_PENDING
    assert state["step_order"] == 1
    assert state["tool_name"] == "tool_x"
    assert state["playbook_id"] == "pb-a"
    assert state["trace_id"] == "trace-a"
    assert isinstance(state["updated_at"], int)


@pytest.mark.asyncio
async def test_get_returns_none_when_payload_is_corrupt():
    """A non-JSON value should be tolerated (returns None) rather than crashing."""
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    sm = StepStateMachine(r)
    key = sm._key("trace-corrupt", "pb-corrupt")
    await r.set(key, "not-valid-json{")
    assert await sm.get("trace-corrupt", "pb-corrupt") is None


@pytest.mark.asyncio
async def test_get_handles_bytes_payload():
    """Cover the `raw.decode()` branch when redis returns bytes."""
    r = fakeredis.aioredis.FakeRedis(decode_responses=False)
    sm = StepStateMachine(r)
    key = sm._key("trace-bytes", "pb-bytes")
    payload = {"status": STEP_PENDING, "step_order": 1, "tool_name": "x",
               "playbook_id": "pb-bytes", "trace_id": "trace-bytes", "updated_at": 1}
    await r.set(key, json.dumps(payload).encode())
    out = await sm.get("trace-bytes", "pb-bytes")
    assert out is not None
    assert out["status"] == STEP_PENDING


# ── init writes state + TTL ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_init_sets_ttl(fake_redis):
    sm = StepStateMachine(fake_redis)
    await sm.init("trace-ttl", "pb-ttl", step_order=2, tool_name="kubectl_rollout_restart")
    ttl = await fake_redis.ttl(sm._key("trace-ttl", "pb-ttl"))
    assert 0 < ttl <= _TTL_SEC


# ── transition: valid / invalid / missing ─────────────────────────────────────

@pytest.mark.asyncio
async def test_transition_pending_to_approved_succeeds(fake_redis):
    sm = StepStateMachine(fake_redis)
    await sm.init("t1", "pb1", step_order=1, tool_name="tool")
    assert await sm.transition("t1", "pb1", STEP_APPROVED) is True
    state = await sm.get("t1", "pb1")
    assert state["status"] == STEP_APPROVED


@pytest.mark.asyncio
async def test_transition_full_happy_path(fake_redis):
    sm = StepStateMachine(fake_redis)
    await sm.init("t2", "pb2", step_order=1, tool_name="tool")
    assert await sm.transition("t2", "pb2", STEP_APPROVED) is True
    assert await sm.transition("t2", "pb2", STEP_EXECUTING) is True
    assert await sm.transition("t2", "pb2", STEP_DONE) is True
    state = await sm.get("t2", "pb2")
    assert state["status"] == STEP_DONE


@pytest.mark.asyncio
async def test_transition_pending_to_rejected_and_expired_paths(fake_redis):
    sm = StepStateMachine(fake_redis)
    await sm.init("t3", "pb3", step_order=1, tool_name="tool")
    assert await sm.transition("t3", "pb3", STEP_REJECTED) is True
    # cannot move out of REJECTED
    assert await sm.transition("t3", "pb3", STEP_APPROVED) is False

    await sm.init("t4", "pb4", step_order=1, tool_name="tool")
    assert await sm.transition("t4", "pb4", STEP_EXPIRED) is True
    assert await sm.transition("t4", "pb4", STEP_DONE) is False


@pytest.mark.asyncio
async def test_transition_executing_to_rejected(fake_redis):
    sm = StepStateMachine(fake_redis)
    await sm.init("t5", "pb5", step_order=1, tool_name="tool")
    await sm.transition("t5", "pb5", STEP_APPROVED)
    await sm.transition("t5", "pb5", STEP_EXECUTING)
    assert await sm.transition("t5", "pb5", STEP_REJECTED) is True


@pytest.mark.asyncio
async def test_transition_rejects_invalid_jump(fake_redis):
    sm = StepStateMachine(fake_redis)
    await sm.init("t6", "pb6", step_order=1, tool_name="tool")
    # PENDING -> EXECUTING is not allowed
    assert await sm.transition("t6", "pb6", STEP_EXECUTING) is False
    state = await sm.get("t6", "pb6")
    assert state["status"] == STEP_PENDING


@pytest.mark.asyncio
async def test_transition_returns_false_when_state_missing(fake_redis):
    sm = StepStateMachine(fake_redis)
    assert await sm.transition("ghost", "ghost-pb", STEP_APPROVED) is False


@pytest.mark.asyncio
async def test_transition_terminal_states_have_no_exits(fake_redis):
    sm = StepStateMachine(fake_redis)
    # DONE state — manually craft, no valid transitions
    await sm.init("t-done", "pb-done", step_order=1, tool_name="x")
    await sm.transition("t-done", "pb-done", STEP_APPROVED)
    await sm.transition("t-done", "pb-done", STEP_EXECUTING)
    await sm.transition("t-done", "pb-done", STEP_DONE)
    # All further transitions blocked
    for target in (STEP_APPROVED, STEP_EXECUTING, STEP_REJECTED, STEP_EXPIRED):
        assert await sm.transition("t-done", "pb-done", target) is False
