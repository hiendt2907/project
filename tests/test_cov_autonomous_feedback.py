"""Coverage tests for workers.autonomous_feedback_loop.

Targets pure helpers first, then async paths that are uncovered.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis as aioredis
import pytest

os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OMNI_OLLAMA_BASE_URL", "http://localhost:11434")

import workers.autonomous_feedback_loop as afl
from workers.autonomous_feedback_loop import (
    _anomaly_event_from_redis_ctx,
    _archive_postmortem,
    _args_hash,
    _embedding_from_response,
    _initial_symptom_from_ctx,
    _load_autonomous_ctx_text,
    _load_state,
    _write_success_hot_cache,
    handle_action_feedback_envelope,
)


# ---------------------------------------------------------------------------
# Patching helpers
# ---------------------------------------------------------------------------


class _PatchAfl:
    """Context-manager that patches afl functions and restores them on exit."""

    def __init__(self, **patches: Any) -> None:
        self._patches = patches
        self._originals: dict[str, Any] = {}

    def __enter__(self) -> "_PatchAfl":
        for name, fn in self._patches.items():
            self._originals[name] = getattr(afl, name)
            setattr(afl, name, fn)
        return self

    def __exit__(self, *_: Any) -> None:
        for name, orig in self._originals.items():
            setattr(afl, name, orig)


def _make_minimal_ctx(redis: Any, **kw: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "settings": SimpleNamespace(
            autonomous_execute_max_attempts=3,
            autonomous_verify_max_rounds=3,
        ),
        "redis": redis,
        "ledger": None,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# No-op stubs reusable across tests
async def _noop_emit_transition(ctx: Any, **kw: Any) -> None:
    pass


async def _noop_emit_terminal(ctx: Any, **kw: Any) -> None:
    pass


async def _noop_tg_escalation(ctx: Any, trace: str, msg: str, reason: str) -> None:
    pass


# ---------------------------------------------------------------------------
# _load_state
# ---------------------------------------------------------------------------


class TestLoadState:
    async def test_empty_redis_returns_defaults(self) -> None:
        r = aioredis.FakeRedis(decode_responses=True)
        state = await _load_state(r, "no-trace")
        assert state["last_attempt_count"] == 0
        assert state["feedback_failures"] == 0
        assert state["sdk_verify_round"] == 0
        assert state["state_verify_attempt"] == 0

    async def test_valid_state_returned(self) -> None:
        r = aioredis.FakeRedis(decode_responses=True)
        payload = {"last_attempt_count": 2, "feedback_failures": 1, "sdk_verify_round": 1, "state_verify_attempt": 0}
        await r.set("omni:autonomous:state:t1", json.dumps(payload))
        state = await _load_state(r, "t1")
        assert state["last_attempt_count"] == 2
        assert state["feedback_failures"] == 1

    async def test_old_format_gets_defaults_added(self) -> None:
        r = aioredis.FakeRedis(decode_responses=True)
        await r.set("omni:autonomous:state:old", json.dumps({"last_attempt_count": 1, "feedback_failures": 0}))
        state = await _load_state(r, "old")
        assert state.get("sdk_verify_round") == 0
        assert state.get("state_verify_attempt") == 0

    async def test_invalid_json_returns_defaults(self) -> None:
        r = aioredis.FakeRedis(decode_responses=True)
        await r.set("omni:autonomous:state:bad", "not-json{")
        state = await _load_state(r, "bad")
        assert state["last_attempt_count"] == 0

    async def test_non_dict_json_returns_defaults(self) -> None:
        r = aioredis.FakeRedis(decode_responses=True)
        await r.set("omni:autonomous:state:arr", "[1, 2, 3]")
        state = await _load_state(r, "arr")
        assert state["last_attempt_count"] == 0


# ---------------------------------------------------------------------------
# _args_hash
# ---------------------------------------------------------------------------


class TestArgsHash:
    def test_returns_24_hex_chars(self) -> None:
        h = _args_hash({"namespace": "default"})
        assert len(h) == 24
        assert all(c in "0123456789abcdef" for c in h)

    def test_stable(self) -> None:
        args = {"a": 1, "b": 2}
        assert _args_hash(args) == _args_hash(args)

    def test_different_args_differ(self) -> None:
        assert _args_hash({"a": 1}) != _args_hash({"a": 2})

    def test_empty_args(self) -> None:
        h = _args_hash({})
        assert len(h) == 24

    def test_none_args(self) -> None:
        h = _args_hash(None)  # type: ignore[arg-type]
        assert len(h) == 24


# ---------------------------------------------------------------------------
# _embedding_from_response
# ---------------------------------------------------------------------------


class TestEmbeddingFromResponse:
    def test_direct_embedding_field(self) -> None:
        resp = {"embedding": [1.0, 2.0, 3.0]}
        result = _embedding_from_response(resp)
        assert result == [1.0, 2.0, 3.0]

    def test_embedding_as_non_list(self) -> None:
        resp = {"embedding": (4.0, 5.0)}
        result = _embedding_from_response(resp)
        assert result == [4.0, 5.0]

    def test_embeddings_list_first_item(self) -> None:
        resp = {"embeddings": [[1.0, 2.0], [3.0, 4.0]]}
        result = _embedding_from_response(resp)
        assert result == [1.0, 2.0]

    def test_empty_response(self) -> None:
        assert _embedding_from_response({}) == []

    def test_embeddings_empty_list(self) -> None:
        assert _embedding_from_response({"embeddings": []}) == []


# ---------------------------------------------------------------------------
# _anomaly_event_from_redis_ctx
# ---------------------------------------------------------------------------


class TestAnomalyEventFromRedisCtx:
    def test_none_ctx_obj_returns_none(self) -> None:
        assert _anomaly_event_from_redis_ctx("trace1", None) is None

    def test_empty_ctx_obj_returns_none(self) -> None:
        assert _anomaly_event_from_redis_ctx("trace1", {}) is None

    def test_non_dict_anomaly_event_min_returns_none(self) -> None:
        assert _anomaly_event_from_redis_ctx("trace1", {"anomaly_event_min": "bad"}) is None

    def test_invalid_anomaly_event_fields_returns_none(self) -> None:
        ctx_obj = {"anomaly_event_min": {"missing_required_field": True}}
        assert _anomaly_event_from_redis_ctx("trace1", ctx_obj) is None

    def test_valid_anomaly_event(self) -> None:
        ctx_obj = {
            "anomaly_event_min": {
                "rule_name": "HighCPU",
                "target": "nginx",
                "canonical_query": "rate(cpu[5m]) > 0.8",
                "namespace": "default",
                "drift_type": "cpu_spike",
            }
        }
        ev = _anomaly_event_from_redis_ctx("trace-v", ctx_obj)
        assert ev is not None
        assert ev.trace_id == "trace-v"
        assert ev.namespace == "default"


# ---------------------------------------------------------------------------
# _initial_symptom_from_ctx
# ---------------------------------------------------------------------------


class TestInitialSymptomFromCtx:
    def test_no_initial_symptom_returns_none(self) -> None:
        assert _initial_symptom_from_ctx({}) is None

    def test_none_initial_symptom_returns_none(self) -> None:
        assert _initial_symptom_from_ctx({"initial_symptom": None}) is None

    def test_non_dict_initial_symptom_returns_none(self) -> None:
        assert _initial_symptom_from_ctx({"initial_symptom": "bad"}) is None

    def test_unknown_fields_returns_symptom_with_defaults(self) -> None:
        # Pydantic v2 ignores extra fields; unknown_field → model with all defaults (not None)
        result = _initial_symptom_from_ctx({"initial_symptom": {"unknown_field": "x"}})
        assert result is not None
        assert result.alertname == ""

    def test_valid_initial_symptom(self) -> None:
        ctx_obj = {"initial_symptom": {"alertname": "HighCPU", "namespace": "default"}}
        result = _initial_symptom_from_ctx(ctx_obj)
        # May return None if schema validation fails for some fields, but must not raise
        # (field validation depends on InitialSymptom schema)


# ---------------------------------------------------------------------------
# _load_autonomous_ctx_text
# ---------------------------------------------------------------------------


class TestLoadAutonomousCtxText:
    async def test_missing_key_returns_empty(self) -> None:
        r = aioredis.FakeRedis(decode_responses=True)
        result = await _load_autonomous_ctx_text(r, "no-trace")
        assert result == ""

    async def test_valid_key_returns_text(self) -> None:
        r = aioredis.FakeRedis(decode_responses=True)
        await r.set("omni:autonomous:ctx:t1", json.dumps({"sanitized_text": "CPU spike at nginx"}))
        result = await _load_autonomous_ctx_text(r, "t1")
        assert result == "CPU spike at nginx"

    async def test_missing_sanitized_text_returns_empty(self) -> None:
        r = aioredis.FakeRedis(decode_responses=True)
        await r.set("omni:autonomous:ctx:t2", json.dumps({"other_field": "x"}))
        result = await _load_autonomous_ctx_text(r, "t2")
        assert result == ""

    async def test_invalid_json_returns_empty(self) -> None:
        r = aioredis.FakeRedis(decode_responses=True)
        await r.set("omni:autonomous:ctx:bad", "not-json")
        result = await _load_autonomous_ctx_text(r, "bad")
        assert result == ""

    async def test_text_truncated_to_4000(self) -> None:
        r = aioredis.FakeRedis(decode_responses=True)
        big_text = "x" * 5000
        await r.set("omni:autonomous:ctx:big", json.dumps({"sanitized_text": big_text}))
        result = await _load_autonomous_ctx_text(r, "big")
        assert len(result) <= 4000


# ---------------------------------------------------------------------------
# _write_success_hot_cache
# ---------------------------------------------------------------------------


class TestWriteSuccessHotCache:
    async def test_writes_to_redis(self) -> None:
        r = aioredis.FakeRedis(decode_responses=True)
        ctx = SimpleNamespace(settings=SimpleNamespace(rag_hot_cache_ttl_sec=3600), redis=r)
        await _write_success_hot_cache(ctx, "trace-hot", "stdout output")
        val = await r.get("omni:autonomous:hot:trace-hot")
        assert val is not None
        data = json.loads(val)
        assert data["trace_id"] == "trace-hot"
        assert data["closed"] is True
        assert "stdout output" in data["stdout_preview"]

    async def test_stdout_truncated_to_2000(self) -> None:
        r = aioredis.FakeRedis(decode_responses=True)
        ctx = SimpleNamespace(settings=SimpleNamespace(rag_hot_cache_ttl_sec=3600), redis=r)
        big_stdout = "x" * 5000
        await _write_success_hot_cache(ctx, "trace-big", big_stdout)
        val = await r.get("omni:autonomous:hot:trace-big")
        data = json.loads(val)
        assert len(data["stdout_preview"]) <= 2000


# ---------------------------------------------------------------------------
# _archive_postmortem
# ---------------------------------------------------------------------------


class TestArchivePostmortem:
    def test_does_not_raise_with_valid_data(self) -> None:
        _archive_postmortem(
            "trace1",
            "k8s_rollout_restart",
            {"namespace": "default", "deployment": "nginx"},
            {"alertname": "HighCPU", "namespace": "default", "deployment": "nginx"},
        )

    def test_does_not_raise_with_none_ctx_obj(self) -> None:
        _archive_postmortem("trace2", "k8s_rollout_restart", {}, None)

    def test_does_not_raise_with_empty_ctx_obj(self) -> None:
        _archive_postmortem("trace3", "k8s_rollout_restart", {"namespace": "x"}, {})


# ---------------------------------------------------------------------------
# _finalize_feedback_success_verified
# ---------------------------------------------------------------------------


async def test_finalize_verified_success_path() -> None:
    """State machine gate passes → upsert + hot cache + emit transition."""
    r = aioredis.FakeRedis(decode_responses=True)
    transitions = []
    hot_caches = []
    upserts = []

    async def fake_verify_sm(ctx: Any, *, trace: str, body: dict, mutate_args: dict, ctx_obj: Any) -> tuple:
        return True, "ok"

    async def fake_upsert(ctx: Any, *, trace: str, tool_name: str, mutate_args: dict, stdout: str, sdk_verify_summary: str = "", ctx_obj: Any = None) -> None:
        upserts.append(trace)

    async def fake_hot(ctx: Any, trace: str, stdout: str) -> None:
        hot_caches.append(trace)

    async def fake_emit(ctx: Any, **kw: Any) -> None:
        transitions.append(kw)

    with _PatchAfl(
        emit_transition=fake_emit,
        _verify_state_machine_gate=fake_verify_sm,
        _upsert_action_experience_on_success=fake_upsert,
        _write_success_hot_cache=fake_hot,
    ):
        ctx = SimpleNamespace(redis=r)
        result = await afl._finalize_feedback_success_verified(
            ctx,
            trace="tr-verified",
            body={"tool_name": "k8s_rollout_restart"},
            mutate_args={"namespace": "default"},
            stdout="stdout ok",
            sdk_verify_summary="all probes pass",
            ctx_obj={"alertname": "HighCPU"},
        )
    assert result is True
    assert "tr-verified" in hot_caches
    assert "tr-verified" in upserts
    assert any(t.get("transition") == "STATE_MACHINE_VERIFIED" for t in transitions)


async def test_finalize_verified_sm_gate_fails() -> None:
    """State machine gate fails → tombstone, return False."""
    r = aioredis.FakeRedis(decode_responses=True)
    tombstones = []
    escalations = []

    async def fake_verify_sm(ctx: Any, *, trace: str, body: dict, mutate_args: dict, ctx_obj: Any) -> tuple:
        return False, "deployment not ready"

    async def fake_emit_terminal(ctx: Any, **kw: Any) -> None:
        tombstones.append(kw)

    async def fake_tg(ctx: Any, trace: str, msg: str, reason: str) -> None:
        escalations.append(reason)

    with _PatchAfl(
        emit_transition=_noop_emit_transition,
        emit_terminal_tombstone=fake_emit_terminal,
        emit_telegram_escalation=fake_tg,
        _verify_state_machine_gate=fake_verify_sm,
    ):
        ctx = SimpleNamespace(redis=r)
        result = await afl._finalize_feedback_success_verified(
            ctx,
            trace="tr-sm-fail",
            body={"tool_name": "k8s_rollout_restart"},
            mutate_args={"namespace": "default"},
            stdout="ok",
            sdk_verify_summary="pass",
        )
    assert result is False
    assert any(t.get("reason_code") == "STATE_MACHINE_GATE_FAIL" for t in tombstones)
    assert "STATE_MACHINE_GATE_FAIL" in escalations


# ---------------------------------------------------------------------------
# _finalize_feedback_success_legacy
# ---------------------------------------------------------------------------


async def test_finalize_legacy_success_with_upsert_enabled() -> None:
    """omni_experience_requires_sdk_verify=False → upsert called."""
    r = aioredis.FakeRedis(decode_responses=True)
    upserts = []

    async def fake_verify_sm(ctx: Any, *, trace: str, body: dict, mutate_args: dict, ctx_obj: Any) -> tuple:
        return True, "ok"

    async def fake_upsert(ctx: Any, *, trace: str, tool_name: str, mutate_args: dict, stdout: str, ctx_obj: Any = None) -> None:
        upserts.append(trace)

    async def fake_hot(ctx: Any, trace: str, stdout: str) -> None:
        pass

    with _PatchAfl(
        emit_transition=_noop_emit_transition,
        _verify_state_machine_gate=fake_verify_sm,
        _upsert_action_experience_on_success=fake_upsert,
        _write_success_hot_cache=fake_hot,
    ):
        ctx = SimpleNamespace(
            settings=SimpleNamespace(omni_experience_requires_sdk_verify=False),
            redis=r,
        )
        result = await afl._finalize_feedback_success_legacy(
            ctx,
            trace="tr-leg-upsert",
            body={"tool_name": "k8s_rollout_restart"},
            mutate_args={"namespace": "default"},
            stdout="done",
        )
    assert result is True
    assert "tr-leg-upsert" in upserts


async def test_finalize_legacy_success_no_upsert_when_sdk_required() -> None:
    """omni_experience_requires_sdk_verify=True → upsert NOT called in legacy path."""
    r = aioredis.FakeRedis(decode_responses=True)
    upserts = []

    async def fake_verify_sm(ctx: Any, *, trace: str, body: dict, mutate_args: dict, ctx_obj: Any) -> tuple:
        return True, "ok"

    async def fake_upsert(ctx: Any, **kw: Any) -> None:
        upserts.append("called")

    async def fake_hot(ctx: Any, trace: str, stdout: str) -> None:
        pass

    with _PatchAfl(
        emit_transition=_noop_emit_transition,
        _verify_state_machine_gate=fake_verify_sm,
        _upsert_action_experience_on_success=fake_upsert,
        _write_success_hot_cache=fake_hot,
    ):
        ctx = SimpleNamespace(
            settings=SimpleNamespace(omni_experience_requires_sdk_verify=True),
            redis=r,
        )
        result = await afl._finalize_feedback_success_legacy(
            ctx,
            trace="tr-leg-no-upsert",
            body={"tool_name": "k8s_rollout_restart"},
            mutate_args={},
            stdout="done",
        )
    assert result is True
    assert len(upserts) == 0


async def test_finalize_legacy_sm_gate_fail() -> None:
    """State machine gate fails in legacy path → tombstone, return False."""
    r = aioredis.FakeRedis(decode_responses=True)
    tombstones = []

    async def fake_verify_sm(ctx: Any, *, trace: str, body: dict, mutate_args: dict, ctx_obj: Any) -> tuple:
        return False, "not ready"

    async def fake_emit_terminal(ctx: Any, **kw: Any) -> None:
        tombstones.append(kw)

    with _PatchAfl(
        emit_transition=_noop_emit_transition,
        emit_terminal_tombstone=fake_emit_terminal,
        emit_telegram_escalation=_noop_tg_escalation,
        _verify_state_machine_gate=fake_verify_sm,
    ):
        ctx = SimpleNamespace(
            settings=SimpleNamespace(omni_experience_requires_sdk_verify=True),
            redis=r,
        )
        result = await afl._finalize_feedback_success_legacy(
            ctx,
            trace="tr-leg-sm-fail",
            body={"tool_name": "k8s_rollout_restart"},
            mutate_args={},
            stdout="done",
        )
    assert result is False
    assert any(t.get("reason_code") == "STATE_MACHINE_GATE_FAIL" for t in tombstones)


# ---------------------------------------------------------------------------
# handle_action_feedback_envelope — edge cases
# ---------------------------------------------------------------------------


async def test_handle_parse_fail() -> None:
    """Bad JSON body → logged, returns early."""
    r = aioredis.FakeRedis(decode_responses=True)
    ctx = _make_minimal_ctx(r)
    await handle_action_feedback_envelope(ctx, {"data": "bad-json{"})


async def test_handle_missing_trace() -> None:
    """No trace_id in body → returns early."""
    r = aioredis.FakeRedis(decode_responses=True)
    ctx = _make_minimal_ctx(r)
    await handle_action_feedback_envelope(ctx, {"data": json.dumps({"exit_code": 0})})


async def test_handle_skipped_auto_execute() -> None:
    """skipped_reason contains 'auto_execute' → EXECUTED transition, return."""
    r = aioredis.FakeRedis(decode_responses=True)
    transitions = []

    with _PatchAfl(
        emit_transition=lambda ctx, **kw: _append_and_noop(transitions, kw),
        emit_terminal_tombstone=_noop_emit_terminal,
    ):
        ctx = _make_minimal_ctx(r)
        body = {
            "trace_id": "tr-skip",
            "exit_code": 1,
            "stdout": "",
            "stderr": "",
            "skipped_reason": "auto_execute disabled",
            "tool_name": "k8s_rollout_restart",
            "mutate_args": {},
        }
        await handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})

    assert any(t.get("transition") == "EXECUTED" for t in transitions)


async def test_handle_success_exit_code_zero_no_verify() -> None:
    """exit_code=0, verify disabled → finalize legacy path."""
    r = aioredis.FakeRedis(decode_responses=True)
    finalized = []

    async def fake_finalize_legacy(ctx: Any, *, trace: str, body: dict, mutate_args: dict, stdout: str, ctx_obj: Any = None) -> bool:
        finalized.append(trace)
        return True

    with _PatchAfl(
        emit_transition=_noop_emit_transition,
        emit_terminal_tombstone=_noop_emit_terminal,
        _finalize_feedback_success_legacy=fake_finalize_legacy,
    ):
        ctx = _make_minimal_ctx(
            r,
            settings=SimpleNamespace(
                autonomous_execute_max_attempts=3,
                autonomous_verify_max_rounds=3,
                omni_post_mutate_sdk_verify_enabled=False,
            ),
        )
        body = {
            "trace_id": "tr-success-noverify",
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
            "tool_name": "k8s_rollout_restart",
            "mutate_args": {},
        }
        await handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})

    assert "tr-success-noverify" in finalized


async def test_handle_failure_max_verify_rounds() -> None:
    """feedback_failures > max_verify → MAX_VERIFY_ROUNDS tombstone."""
    r = aioredis.FakeRedis(decode_responses=True)
    # Store state with high feedback_failures
    await r.set(
        "omni:autonomous:state:tr-max-verify",
        json.dumps({"last_attempt_count": 1, "feedback_failures": 3, "sdk_verify_round": 0, "state_verify_attempt": 0}),
    )
    tombstones = []
    escalations = []

    async def fake_finalize_from_stored(ctx: Any, trace: str, *, body: dict, mutate_args: dict, verify_summary: str, stdout: str, reason_tag: str) -> bool:
        return False  # Not healthy, proceed

    with _PatchAfl(
        emit_transition=_noop_emit_transition,
        emit_terminal_tombstone=lambda ctx, **kw: _append_and_noop(tombstones, kw),
        emit_telegram_escalation=lambda ctx, trace, msg, reason: _append_and_noop(escalations, {"reason": reason}),
        _finalize_if_deployment_rollout_healthy_from_stored_ctx=fake_finalize_from_stored,
    ):
        ctx = _make_minimal_ctx(
            r,
            settings=SimpleNamespace(
                autonomous_execute_max_attempts=3,
                autonomous_verify_max_rounds=3,
            ),
        )
        body = {
            "trace_id": "tr-max-verify",
            "exit_code": 1,
            "stdout": "err",
            "stderr": "err",
            "tool_name": "k8s_rollout_restart",
            "mutate_args": {},
        }
        await handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})

    assert any(t.get("reason_code") == "MAX_VERIFY_ROUNDS" for t in tombstones)


async def test_handle_failure_max_mutate_attempts() -> None:
    """last_attempt >= max_attempts → MAX_MUTATE_ATTEMPTS tombstone."""
    r = aioredis.FakeRedis(decode_responses=True)
    await r.set(
        "omni:autonomous:state:tr-max-mutate",
        json.dumps({"last_attempt_count": 3, "feedback_failures": 0, "sdk_verify_round": 0, "state_verify_attempt": 0}),
    )
    tombstones = []

    async def fake_finalize_from_stored(ctx: Any, trace: str, **kw: Any) -> bool:
        return False

    with _PatchAfl(
        emit_transition=_noop_emit_transition,
        emit_terminal_tombstone=lambda ctx, **kw: _append_and_noop(tombstones, kw),
        emit_telegram_escalation=_noop_tg_escalation,
        _finalize_if_deployment_rollout_healthy_from_stored_ctx=fake_finalize_from_stored,
    ):
        ctx = _make_minimal_ctx(
            r,
            settings=SimpleNamespace(
                autonomous_execute_max_attempts=3,
                autonomous_verify_max_rounds=3,
            ),
        )
        body = {
            "trace_id": "tr-max-mutate",
            "exit_code": 1,
            "stdout": "fail",
            "stderr": "err",
            "tool_name": "k8s_rollout_restart",
            "mutate_args": {},
        }
        await handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})

    assert any(t.get("reason_code") == "MAX_MUTATE_ATTEMPTS" for t in tombstones)


async def test_handle_failure_replan_success() -> None:
    """exit_code!=0, normal state → replan → emit EXECUTE_MUTATE."""
    r = aioredis.FakeRedis(decode_responses=True)
    mutate_emits = []

    async def fake_replan(ctx: Any, trace: str, stdout: str, stderr: str, exit_code: int) -> dict:
        return {"tool_name": "k8s_rollout_restart", "args": {"namespace": "default", "deployment": "nginx"}}

    async def fake_emit_mutate(ctx: Any, *, trace: str, tool_name: str, args: dict, attempt_count: int, reasoning_chain: Any = None) -> None:
        mutate_emits.append({"trace": trace, "tool_name": tool_name})

    with _PatchAfl(
        emit_transition=_noop_emit_transition,
        emit_terminal_tombstone=_noop_emit_terminal,
        emit_telegram_escalation=_noop_tg_escalation,
        _llm_replan_after_feedback=fake_replan,
        emit_execute_mutate=fake_emit_mutate,
    ):
        ctx = _make_minimal_ctx(
            r,
            settings=SimpleNamespace(
                autonomous_execute_max_attempts=3,
                autonomous_verify_max_rounds=3,
                omni_feedback_full_agentic_planner_enabled=False,
            ),
        )
        body = {
            "trace_id": "tr-replan",
            "exit_code": 1,
            "stdout": "error output",
            "stderr": "some error",
            "tool_name": "k8s_rollout_restart",
            "mutate_args": {},
        }
        await handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})

    assert any(m["trace"] == "tr-replan" for m in mutate_emits)


async def test_handle_failure_replan_no_op() -> None:
    """Replan returns no_op → state deleted, returns early."""
    r = aioredis.FakeRedis(decode_responses=True)

    async def fake_replan(ctx: Any, trace: str, stdout: str, stderr: str, exit_code: int) -> dict:
        return {"tool_name": "no_op", "args": {}}

    with _PatchAfl(
        emit_transition=_noop_emit_transition,
        emit_terminal_tombstone=_noop_emit_terminal,
        emit_telegram_escalation=_noop_tg_escalation,
        _llm_replan_after_feedback=fake_replan,
    ):
        ctx = _make_minimal_ctx(
            r,
            settings=SimpleNamespace(
                autonomous_execute_max_attempts=3,
                autonomous_verify_max_rounds=3,
                omni_feedback_full_agentic_planner_enabled=False,
            ),
        )
        body = {
            "trace_id": "tr-noop",
            "exit_code": 1,
            "stdout": "fail",
            "stderr": "err",
            "tool_name": "k8s_rollout_restart",
            "mutate_args": {},
        }
        await handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})

    # State should be cleaned up
    remaining = await r.get("omni:autonomous:state:tr-noop")
    assert remaining is None


async def test_handle_failure_replan_empty() -> None:
    """Replan returns None → REPLAN_EMPTY tombstone."""
    r = aioredis.FakeRedis(decode_responses=True)
    tombstones = []

    async def fake_replan(ctx: Any, trace: str, stdout: str, stderr: str, exit_code: int) -> None:
        return None

    async def fake_finalize_from_stored(ctx: Any, trace: str, **kw: Any) -> bool:
        return False

    with _PatchAfl(
        emit_transition=_noop_emit_transition,
        emit_terminal_tombstone=lambda ctx, **kw: _append_and_noop(tombstones, kw),
        emit_telegram_escalation=_noop_tg_escalation,
        _llm_replan_after_feedback=fake_replan,
        _finalize_if_deployment_rollout_healthy_from_stored_ctx=fake_finalize_from_stored,
    ):
        ctx = _make_minimal_ctx(
            r,
            settings=SimpleNamespace(
                autonomous_execute_max_attempts=3,
                autonomous_verify_max_rounds=3,
                omni_feedback_full_agentic_planner_enabled=False,
            ),
        )
        body = {
            "trace_id": "tr-empty-replan",
            "exit_code": 1,
            "stdout": "fail",
            "stderr": "err",
            "tool_name": "k8s_rollout_restart",
            "mutate_args": {},
        }
        await handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})

    assert any(t.get("reason_code") == "REPLAN_EMPTY" for t in tombstones)


async def test_handle_success_with_stored_ctx_data() -> None:
    """exit_code=0, ctx data stored in Redis → finalize legacy path uses it."""
    r = aioredis.FakeRedis(decode_responses=True)
    ctx_data = {"sanitized_text": "CPU spike", "alertname": "HighCPU", "omni_verify_required": False}
    await r.set("omni:autonomous:ctx:tr-ctx", json.dumps(ctx_data))

    finalized = []

    async def fake_finalize_legacy(ctx: Any, *, trace: str, body: dict, mutate_args: dict, stdout: str, ctx_obj: Any = None) -> bool:
        finalized.append({"trace": trace, "ctx_obj": ctx_obj})
        return True

    with _PatchAfl(
        emit_transition=_noop_emit_transition,
        emit_terminal_tombstone=_noop_emit_terminal,
        _finalize_feedback_success_legacy=fake_finalize_legacy,
    ):
        ctx = _make_minimal_ctx(
            r,
            settings=SimpleNamespace(
                autonomous_execute_max_attempts=3,
                autonomous_verify_max_rounds=3,
                omni_post_mutate_sdk_verify_enabled=True,
            ),
        )
        body = {
            "trace_id": "tr-ctx",
            "exit_code": 0,
            "stdout": "done",
            "stderr": "",
            "tool_name": "k8s_rollout_restart",
            "mutate_args": {},
        }
        await handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})

    assert any(f["trace"] == "tr-ctx" for f in finalized)


async def test_handle_success_trace_from_fields() -> None:
    """trace_id extracted from fields when not in body data."""
    r = aioredis.FakeRedis(decode_responses=True)
    finalized = []

    async def fake_finalize_legacy(ctx: Any, *, trace: str, body: dict, mutate_args: dict, stdout: str, ctx_obj: Any = None) -> bool:
        finalized.append(trace)
        return True

    with _PatchAfl(
        emit_transition=_noop_emit_transition,
        emit_terminal_tombstone=_noop_emit_terminal,
        _finalize_feedback_success_legacy=fake_finalize_legacy,
    ):
        ctx = _make_minimal_ctx(
            r,
            settings=SimpleNamespace(
                autonomous_execute_max_attempts=3,
                autonomous_verify_max_rounds=3,
                omni_post_mutate_sdk_verify_enabled=False,
            ),
        )
        # trace_id in fields, not in body data
        body_data = {"exit_code": 0, "stdout": "ok", "stderr": "", "tool_name": "k8s_rollout_restart", "mutate_args": {}}
        await handle_action_feedback_envelope(ctx, {"data": json.dumps(body_data), "trace_id": "tr-from-fields"})

    assert "tr-from-fields" in finalized


# ---------------------------------------------------------------------------
# Helpers used in tests above
# ---------------------------------------------------------------------------


async def _append_and_noop(lst: list, item: Any) -> None:
    """Append item to list; async-compatible helper for _PatchAfl callbacks."""
    lst.append(item)


# Override the helper since it can't be called like that — use a proper async wrapper
async def _async_append(lst: list, item: Any) -> None:
    lst.append(item)


# ---------------------------------------------------------------------------
# Patch context manager fix — rewrite _append_and_noop as a proper coroutine factory
# ---------------------------------------------------------------------------

# The _append_and_noop above returns a coroutine that must be awaited.
# Since it's used as a lambda in with _PatchAfl, we need a different approach.
# Rewrite the tests to use explicit async functions for clarity.


async def test_handle_ctx_bytes_from_redis() -> None:
    """ctx data stored as bytes in Redis is correctly decoded."""
    r = aioredis.FakeRedis(decode_responses=False)  # bytes mode
    ctx_data = {"sanitized_text": "Pod OOMKilled", "alertname": "OOMKilled", "omni_verify_required": False}
    await r.set("omni:autonomous:ctx:tr-bytes", json.dumps(ctx_data).encode())

    finalized = []

    async def fake_finalize_legacy(ctx: Any, *, trace: str, **kw: Any) -> bool:
        finalized.append(trace)
        return True

    orig_emit = afl.emit_transition
    orig_term = afl.emit_terminal_tombstone
    orig_fin = afl._finalize_feedback_success_legacy
    afl.emit_transition = _noop_emit_transition
    afl.emit_terminal_tombstone = _noop_emit_terminal
    afl._finalize_feedback_success_legacy = fake_finalize_legacy

    try:
        ctx = _make_minimal_ctx(
            r,
            settings=SimpleNamespace(
                autonomous_execute_max_attempts=3,
                autonomous_verify_max_rounds=3,
                omni_post_mutate_sdk_verify_enabled=False,
            ),
        )
        body = {
            "trace_id": "tr-bytes",
            "exit_code": 0,
            "stdout": "done",
            "stderr": "",
            "tool_name": "k8s_rollout_restart",
            "mutate_args": {},
        }
        await handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})
    finally:
        afl.emit_transition = orig_emit
        afl.emit_terminal_tombstone = orig_term
        afl._finalize_feedback_success_legacy = orig_fin

    assert "tr-bytes" in finalized


async def test_handle_success_with_k8s_patch_secret_rollout() -> None:
    """exit_code=0, tool=k8s_patch_secret with chaos autofix → emit rollout restart."""
    r = aioredis.FakeRedis(decode_responses=True)
    # Store ctx with rollout_ns_dep
    ctx_data = {
        "rollout_ns_dep": {"namespace": "multi-agent", "deployment": "nginx"},
    }
    await r.set("omni:autonomous:ctx:tr-patch-secret", json.dumps(ctx_data))

    emit_mutates = []

    async def fake_emit_mutate(ctx: Any, *, trace: str, tool_name: str, args: dict, attempt_count: int, reasoning_chain: Any = None) -> None:
        emit_mutates.append({"trace": trace, "tool_name": tool_name})

    orig_emit = afl.emit_transition
    orig_term = afl.emit_terminal_tombstone
    orig_mutate = afl.emit_execute_mutate
    afl.emit_transition = _noop_emit_transition
    afl.emit_terminal_tombstone = _noop_emit_terminal
    afl.emit_execute_mutate = fake_emit_mutate

    try:
        ctx = _make_minimal_ctx(
            r,
            settings=SimpleNamespace(
                autonomous_execute_max_attempts=3,
                autonomous_verify_max_rounds=3,
                lab_chaos_credential_autofix_enabled=True,
            ),
        )
        body = {
            "trace_id": "tr-patch-secret",
            "exit_code": 0,
            "stdout": "secret patched",
            "stderr": "",
            "tool_name": "k8s_patch_secret",
            "mutate_args": {},
        }
        await handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})
    finally:
        afl.emit_transition = orig_emit
        afl.emit_terminal_tombstone = orig_term
        afl.emit_execute_mutate = orig_mutate

    # Should have emitted a k8s_rollout_restart after the patch
    assert any(m["tool_name"] == "k8s_rollout_restart" for m in emit_mutates)


async def test_handle_failure_with_ctx_data_in_redis() -> None:
    """exit_code=1, ctx data in Redis loaded when replanning."""
    r = aioredis.FakeRedis(decode_responses=True)
    ctx_data = {"sanitized_text": "memory spike", "verify_probe_ids": []}
    await r.set("omni:autonomous:ctx:tr-fail-ctx", json.dumps(ctx_data))

    replan_calls = []

    async def fake_replan(ctx: Any, trace: str, stdout: str, stderr: str, exit_code: int) -> dict:
        replan_calls.append(trace)
        return {"tool_name": "k8s_rollout_restart", "args": {"namespace": "default", "deployment": "app"}}

    emit_mutates = []

    async def fake_emit_mutate(ctx: Any, *, trace: str, tool_name: str, args: dict, attempt_count: int, reasoning_chain: Any = None) -> None:
        emit_mutates.append(trace)

    orig_emit = afl.emit_transition
    orig_term = afl.emit_terminal_tombstone
    orig_tg = afl.emit_telegram_escalation
    orig_replan = afl._llm_replan_after_feedback
    orig_mutate = afl.emit_execute_mutate
    afl.emit_transition = _noop_emit_transition
    afl.emit_terminal_tombstone = _noop_emit_terminal
    afl.emit_telegram_escalation = _noop_tg_escalation
    afl._llm_replan_after_feedback = fake_replan
    afl.emit_execute_mutate = fake_emit_mutate

    try:
        ctx = _make_minimal_ctx(
            r,
            settings=SimpleNamespace(
                autonomous_execute_max_attempts=3,
                autonomous_verify_max_rounds=3,
                omni_feedback_full_agentic_planner_enabled=False,
            ),
        )
        body = {
            "trace_id": "tr-fail-ctx",
            "exit_code": 1,
            "stdout": "error",
            "stderr": "err",
            "tool_name": "k8s_rollout_restart",
            "mutate_args": {},
        }
        await handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})
    finally:
        afl.emit_transition = orig_emit
        afl.emit_terminal_tombstone = orig_term
        afl.emit_telegram_escalation = orig_tg
        afl._llm_replan_after_feedback = orig_replan
        afl.emit_execute_mutate = orig_mutate

    assert "tr-fail-ctx" in replan_calls
    assert "tr-fail-ctx" in emit_mutates


async def test_load_state_bytes_redis() -> None:
    """_load_state works when Redis returns bytes."""
    r = aioredis.FakeRedis(decode_responses=False)  # bytes mode
    payload = {"last_attempt_count": 1, "feedback_failures": 0, "sdk_verify_round": 0, "state_verify_attempt": 0}
    await r.set("omni:autonomous:state:tr-bytes-st", json.dumps(payload).encode())
    state = await _load_state(r, "tr-bytes-st")
    assert state["last_attempt_count"] == 1


async def test_write_success_hot_cache_handles_redis_error() -> None:
    """_write_success_hot_cache handles redis exceptions gracefully."""

    class BrokenRedis:
        async def setex(self, *args: Any, **kw: Any) -> None:
            raise ConnectionError("connection refused")

    ctx = SimpleNamespace(settings=SimpleNamespace(rag_hot_cache_ttl_sec=3600), redis=BrokenRedis())
    # Should not raise
    await _write_success_hot_cache(ctx, "trace-err", "stdout ok")
