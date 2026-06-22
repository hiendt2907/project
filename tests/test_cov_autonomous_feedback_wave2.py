"""Coverage wave-2 tests for workers.autonomous_feedback_loop.

Targets uncovered lines/branches not covered by test_cov_autonomous_feedback.py:
  - _upsert_action_experience_on_success: full path, embed dimension padding, exception path
  - _verify_state_machine_gate: full path (healthy/unhealthy), no anomaly event, no ns/dep
  - _finalize_if_deployment_rollout_healthy: settings disabled, missing ns/dep, healthy/unhealthy
  - _finalize_if_deployment_rollout_healthy_from_stored_ctx: missing ctx, invalid json, no anomaly ev
  - _llm_post_verify_state_react: disabled, max_attempts exceeded, empty plan, no tool_name, emits
  - _llm_replan_after_feedback: LLM success paths, exception, no_op result, no valid k8s plan
  - handle_action_feedback_envelope: k8s_patch_secret chaos lab path, k8s_rollout_restart chaos path,
      replan no_op path, replan empty path, attempt_count_exceeded path
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis as aioredis
import pytest

os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OMNI_OLLAMA_BASE_URL", "http://localhost:11434")

import workers.autonomous_feedback_loop as afl
from workers.autonomous_feedback_loop import (
    _load_state,
    _verify_state_machine_gate,
    _finalize_if_deployment_rollout_healthy,
    _finalize_if_deployment_rollout_healthy_from_stored_ctx,
    _llm_post_verify_state_react,
    _llm_replan_after_feedback,
    _upsert_action_experience_on_success,
    handle_action_feedback_envelope,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _PatchAfl:
    """Patch afl module attrs, restore on exit."""

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


async def _noop(*args: Any, **kwargs: Any) -> None:
    pass


async def _noop_bool(*args: Any, **kwargs: Any) -> bool:
    return False


def _async_capture(lst: list[Any]) -> Any:
    async def _fn(ctx: Any, **kw: Any) -> None:
        lst.append(kw)

    return _fn


def _make_settings(**kw: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "autonomous_execute_max_attempts": 3,
        "autonomous_verify_max_rounds": 3,
        "rag_hot_cache_ttl_sec": 3600,
        "embed_model": "nomic-embed-text",
        "omni_experience_requires_sdk_verify": True,
        "omni_post_verify_deployment_state_enabled": True,
        "omni_telegram_suppress_when_deployment_healthy": True,
        "omni_post_verify_state_llm_enabled": True,
        "omni_post_mutate_sdk_verify_enabled": True,
        "omni_post_mutate_verify_planner_enabled": False,
        "omni_feedback_full_agentic_planner_enabled": False,
        "omni_llm_first_autonomy_enabled": False,
        "omni_legacy_deterministic_fallback": False,
        "lab_chaos_credential_autofix_enabled": False,
        "diag_evidence_llm_model": "qwen2.5:7b",
        "chat_model": "qwen2.5-coder-3b",
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_ctx(redis: Any = None, **kw: Any) -> SimpleNamespace:
    if redis is None:
        redis = aioredis.FakeRedis(decode_responses=True)
    merged = kw.pop("settings", None)
    settings = merged if merged is not None else _make_settings()
    base: dict[str, Any] = {
        "redis": redis,
        "settings": settings,
        "kafka": None,
        "ledger": None,
        "inbound_trace_id": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _valid_anomaly_event_min() -> dict:
    return {
        "rule_name": "HighCPU",
        "target": "nginx",
        "canonical_query": "rate(cpu[5m]) > 0.8",
        "namespace": "default",
        "drift_type": "cpu_spike",
    }


# ---------------------------------------------------------------------------
# _upsert_action_experience_on_success
# ---------------------------------------------------------------------------


class TestUpsertActionExperienceOnSuccess:
    async def test_success_path_with_embedding(self):
        """Full path: embed returns correct dim → upsert called."""
        r = aioredis.FakeRedis(decode_responses=True)
        upserts = []
        EMBED_DIM = 768

        mock_llm = AsyncMock()
        mock_llm.embed = AsyncMock(return_value={"embedding": [0.1] * EMBED_DIM})
        mock_vs = AsyncMock()
        mock_vs.upsert = AsyncMock(side_effect=lambda **kw: upserts.append(kw))

        ctx = _make_ctx(redis=r)
        ctx.llm = mock_llm
        ctx.vector_store = mock_vs
        ctx.settings = _make_settings(embed_model="nomic-embed-text")

        with patch("workers.autonomous_feedback_loop.inc_learning_upsert"), \
             patch("workers.autonomous_feedback_loop.inc_experience_saved"):
            await _upsert_action_experience_on_success(
                ctx,
                trace="t1",
                tool_name="k8s_rollout_restart",
                mutate_args={"namespace": "default", "deployment": "nginx"},
                stdout="success output",
                sdk_verify_summary="all probes passed",
                ctx_obj={"alertname": "HighCPU", "drift_type": "cpu_spike"},
            )
        assert len(upserts) == 1

    async def test_embed_dim_padding_applied(self):
        """embed returns short vector → padded to EMBED_DIM."""
        r = aioredis.FakeRedis(decode_responses=True)
        upserts = []
        EMBED_DIM = 768

        mock_llm = AsyncMock()
        # Return only 2 dims — must be padded
        mock_llm.embed = AsyncMock(return_value={"embedding": [0.5, 0.5]})
        mock_vs = AsyncMock()
        mock_vs.upsert = AsyncMock(side_effect=lambda **kw: upserts.append(kw))

        ctx = _make_ctx(redis=r)
        ctx.llm = mock_llm
        ctx.vector_store = mock_vs
        ctx.settings = _make_settings(embed_model="nomic-embed-text")

        with patch("workers.autonomous_feedback_loop.inc_learning_upsert"), \
             patch("workers.autonomous_feedback_loop.inc_experience_saved"):
            await _upsert_action_experience_on_success(
                ctx,
                trace="t2",
                tool_name="k8s_rollout_restart",
                mutate_args={},
                stdout="",
                sdk_verify_summary="",
            )
        assert len(upserts) == 1
        vec = upserts[0]["points"][0].vector
        assert len(vec) == EMBED_DIM

    async def test_exception_logs_failure_metric(self):
        """LLM embed raises → exception caught → inc_learning_upsert(fail) called."""
        r = aioredis.FakeRedis(decode_responses=True)
        mock_llm = AsyncMock()
        mock_llm.embed = AsyncMock(side_effect=RuntimeError("LLM down"))

        ctx = _make_ctx(redis=r)
        ctx.llm = mock_llm
        ctx.settings = _make_settings(embed_model="nomic-embed-text")

        fail_calls = []
        with patch("workers.autonomous_feedback_loop.inc_learning_upsert",
                   side_effect=lambda *a: fail_calls.append(a)):
            # Must not raise
            await _upsert_action_experience_on_success(
                ctx,
                trace="t3",
                tool_name="k8s_rollout_restart",
                mutate_args={},
                stdout="",
                sdk_verify_summary="",
            )
        assert any(a[1] == "fail" for a in fail_calls)

    async def test_sdk_verify_summary_appended_to_extra(self):
        """sdk_verify_summary triggers extra block in symptom_raw."""
        r = aioredis.FakeRedis(decode_responses=True)
        upserts = []
        EMBED_DIM = 768

        mock_llm = AsyncMock()
        mock_llm.embed = AsyncMock(return_value={"embeddings": [[0.1] * EMBED_DIM]})
        mock_vs = AsyncMock()
        mock_vs.upsert = AsyncMock(side_effect=lambda **kw: upserts.append(kw))

        ctx = _make_ctx(redis=r)
        ctx.llm = mock_llm
        ctx.vector_store = mock_vs
        ctx.settings = _make_settings(embed_model="nomic-embed-text")

        with patch("workers.autonomous_feedback_loop.inc_learning_upsert"), \
             patch("workers.autonomous_feedback_loop.inc_experience_saved"):
            await _upsert_action_experience_on_success(
                ctx,
                trace="t4",
                tool_name="k8s_rollout_restart",
                mutate_args={},
                stdout="done",
                sdk_verify_summary="probe ok",  # non-empty summary
            )
        assert len(upserts) == 1


# ---------------------------------------------------------------------------
# _verify_state_machine_gate
# ---------------------------------------------------------------------------


class TestVerifyStateMachineGate:
    async def test_no_anomaly_event_returns_true(self):
        """ctx_obj without anomaly_event_min → True (gate not applicable)."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=r)
        with patch("workers.autonomous_feedback_loop.emit_transition", new=AsyncMock()):
            ok, detail = await _verify_state_machine_gate(
                ctx,
                trace="t1",
                body={"tool_name": "k8s_rollout_restart"},
                mutate_args={"namespace": "ns1"},
                ctx_obj={},  # no anomaly_event_min
            )
        assert ok is True
        assert "not_applicable" in detail

    async def test_missing_ns_or_dep_returns_true(self):
        """anomaly_event present but resolve_namespace returns empty → True."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=r)
        ctx_obj = {"anomaly_event_min": _valid_anomaly_event_min()}
        with patch("workers.autonomous_feedback_loop.emit_transition", new=AsyncMock()), \
             patch("workers.autonomous_feedback_loop.resolve_namespace_deployment_for_state_gate",
                   return_value=("", "")):
            ok, detail = await _verify_state_machine_gate(
                ctx,
                trace="trace-t2",
                body={"tool_name": "k8s_rollout_restart"},
                mutate_args={},
                ctx_obj=ctx_obj,
            )
        assert ok is True
        assert "not_applicable" in detail

    async def test_deployment_healthy_returns_true(self):
        """check_deployment_rollout_healthy=True → True."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=r)
        ctx_obj = {"anomaly_event_min": _valid_anomaly_event_min()}
        with patch("workers.autonomous_feedback_loop.emit_transition", new=AsyncMock()), \
             patch("workers.autonomous_feedback_loop.resolve_namespace_deployment_for_state_gate",
                   return_value=("ns1", "nginx")), \
             patch("workers.autonomous_feedback_loop.check_deployment_rollout_healthy",
                   new=AsyncMock(return_value=(True, "replicas ok"))):
            ok, detail = await _verify_state_machine_gate(
                ctx,
                trace="trace-t3",
                body={"tool_name": "k8s_rollout_restart"},
                mutate_args={"namespace": "ns1"},
                ctx_obj=ctx_obj,
            )
        assert ok is True
        assert "replicas ok" in detail

    async def test_deployment_unhealthy_returns_false(self):
        """check_deployment_rollout_healthy=False → False."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=r)
        ctx_obj = {"anomaly_event_min": _valid_anomaly_event_min()}
        with patch("workers.autonomous_feedback_loop.emit_transition", new=AsyncMock()), \
             patch("workers.autonomous_feedback_loop.resolve_namespace_deployment_for_state_gate",
                   return_value=("ns1", "nginx")), \
             patch("workers.autonomous_feedback_loop.check_deployment_rollout_healthy",
                   new=AsyncMock(return_value=(False, "pods not ready"))):
            ok, detail = await _verify_state_machine_gate(
                ctx,
                trace="trace-t4",
                body={"tool_name": "k8s_rollout_restart"},
                mutate_args={"namespace": "ns1"},
                ctx_obj=ctx_obj,
            )
        assert ok is False
        assert "pods not ready" in detail


# ---------------------------------------------------------------------------
# _finalize_if_deployment_rollout_healthy
# ---------------------------------------------------------------------------


class TestFinalizeIfDeploymentRolloutHealthy:
    def _ev(self):
        from workers.proactive_models import AnomalyEvent
        d = dict(_valid_anomaly_event_min())
        d["trace_id"] = "trace-x"
        return AnomalyEvent.model_validate(d)

    async def test_disabled_by_setting_returns_false(self):
        """omni_post_verify_deployment_state_enabled=False → False."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=r)
        ctx.settings = _make_settings(omni_post_verify_deployment_state_enabled=False)
        result = await _finalize_if_deployment_rollout_healthy(
            ctx, "t1",
            body={"tool_name": "k8s_rollout_restart"},
            mutate_args={},
            ctx_obj=None,
            verify_summary="",
            stdout="ok",
            ev=self._ev(),
            reason_tag="TEST",
        )
        assert result is False

    async def test_suppress_disabled_returns_false(self):
        """omni_telegram_suppress_when_deployment_healthy=False → False."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=r)
        ctx.settings = _make_settings(omni_telegram_suppress_when_deployment_healthy=False)
        result = await _finalize_if_deployment_rollout_healthy(
            ctx, "t2",
            body={"tool_name": "k8s_rollout_restart"},
            mutate_args={},
            ctx_obj=None,
            verify_summary="",
            stdout="ok",
            ev=self._ev(),
            reason_tag="TEST",
        )
        assert result is False

    async def test_missing_ns_dep_returns_false(self):
        """resolve_namespace returns empty strings → False."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=r)
        with patch("workers.autonomous_feedback_loop.resolve_namespace_deployment_for_state_gate",
                   return_value=("", "")):
            result = await _finalize_if_deployment_rollout_healthy(
                ctx, "t3",
                body={"tool_name": "k8s_rollout_restart"},
                mutate_args={},
                ctx_obj=None,
                verify_summary="",
                stdout="ok",
                ev=self._ev(),
                reason_tag="TEST",
            )
        assert result is False

    async def test_unhealthy_deployment_returns_false(self):
        """check_deployment_rollout_healthy=False → False."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=r)
        with patch("workers.autonomous_feedback_loop.resolve_namespace_deployment_for_state_gate",
                   return_value=("ns1", "nginx")), \
             patch("workers.autonomous_feedback_loop.check_deployment_rollout_healthy",
                   new=AsyncMock(return_value=(False, "not ready"))):
            result = await _finalize_if_deployment_rollout_healthy(
                ctx, "t4",
                body={"tool_name": "k8s_rollout_restart"},
                mutate_args={},
                ctx_obj=None,
                verify_summary="",
                stdout="ok",
                ev=self._ev(),
                reason_tag="TEST",
            )
        assert result is False

    async def test_healthy_deployment_finalizes_and_returns_true(self):
        """Healthy deployment → calls _finalize_feedback_success_verified → True."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=r)
        finalized = []

        async def fake_finalize(ctx, *, trace, body, mutate_args, stdout, sdk_verify_summary, ctx_obj=None):
            finalized.append(trace)
            return True

        with patch("workers.autonomous_feedback_loop.resolve_namespace_deployment_for_state_gate",
                   return_value=("ns1", "nginx")), \
             patch("workers.autonomous_feedback_loop.check_deployment_rollout_healthy",
                   new=AsyncMock(return_value=(True, "all ok"))), \
             _PatchAfl(_finalize_feedback_success_verified=fake_finalize):
            result = await _finalize_if_deployment_rollout_healthy(
                ctx, "t-health-ok",
                body={"tool_name": "k8s_rollout_restart"},
                mutate_args={"namespace": "ns1"},
                ctx_obj={"alertname": "HighCPU"},
                verify_summary="probes ok",
                stdout="ok output",
                ev=self._ev(),
                reason_tag="SDK_VERIFY_EXHAUSTED",
            )
        assert result is True
        assert "t-health-ok" in finalized


# ---------------------------------------------------------------------------
# _finalize_if_deployment_rollout_healthy_from_stored_ctx
# ---------------------------------------------------------------------------


class TestFinalizeFromStoredCtx:
    async def test_missing_redis_key_returns_false(self):
        """No ctx stored in redis → ev is None → False."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=r)
        result = await _finalize_if_deployment_rollout_healthy_from_stored_ctx(
            ctx, "t-no-ctx",
            body={},
            mutate_args={},
            verify_summary="",
            stdout="",
            reason_tag="TEST",
        )
        assert result is False

    async def test_invalid_json_in_redis_returns_false(self):
        """Invalid JSON stored → ctx_obj={} → ev is None → False."""
        r = aioredis.FakeRedis(decode_responses=True)
        await r.set("omni:autonomous:ctx:t-bad", "not-json{{{")
        ctx = _make_ctx(redis=r)
        result = await _finalize_if_deployment_rollout_healthy_from_stored_ctx(
            ctx, "t-bad",
            body={},
            mutate_args={},
            verify_summary="",
            stdout="",
            reason_tag="TEST",
        )
        assert result is False

    async def test_ctx_without_anomaly_event_min_returns_false(self):
        """ctx_obj has no anomaly_event_min → ev=None → False."""
        r = aioredis.FakeRedis(decode_responses=True)
        await r.set("omni:autonomous:ctx:t-no-ev", json.dumps({"other": "data"}))
        ctx = _make_ctx(redis=r)
        result = await _finalize_if_deployment_rollout_healthy_from_stored_ctx(
            ctx, "t-no-ev",
            body={},
            mutate_args={},
            verify_summary="",
            stdout="",
            reason_tag="TEST",
        )
        assert result is False

    async def test_ctx_with_valid_event_delegates_to_finalize(self):
        """Valid ctx with anomaly_event_min → delegates to _finalize_if_deployment_rollout_healthy."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx_data = {"anomaly_event_min": _valid_anomaly_event_min()}
        await r.set("omni:autonomous:ctx:t-ok", json.dumps(ctx_data))
        ctx = _make_ctx(redis=r)
        calls = []

        async def fake_finalize_health(ctx, trace, *, body, mutate_args, ctx_obj, verify_summary, stdout, ev, reason_tag):
            calls.append(reason_tag)
            return True

        with _PatchAfl(_finalize_if_deployment_rollout_healthy=fake_finalize_health):
            result = await _finalize_if_deployment_rollout_healthy_from_stored_ctx(
                ctx, "t-ok",
                body={},
                mutate_args={},
                verify_summary="",
                stdout="",
                reason_tag="MAX_VERIFY_ROUNDS",
            )
        assert result is True
        assert "MAX_VERIFY_ROUNDS" in calls


# ---------------------------------------------------------------------------
# _llm_post_verify_state_react
# ---------------------------------------------------------------------------


class TestLlmPostVerifyStateReact:
    async def test_disabled_by_setting_returns_false(self):
        """omni_post_verify_state_llm_enabled=False → False immediately."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=r)
        ctx.settings = _make_settings(omni_post_verify_state_llm_enabled=False)
        result = await _llm_post_verify_state_react(
            ctx,
            trace="t1",
            namespace="ns1",
            deployment="nginx",
            verify_summary="",
            dep_detail="",
            stdout="",
            last_attempt=0,
        )
        assert result is False

    async def test_max_attempts_exceeded_returns_false(self):
        """next_attempt > max_attempts → False."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=r)
        ctx.settings = _make_settings(omni_post_verify_state_llm_enabled=True,
                                       autonomous_execute_max_attempts=3)
        result = await _llm_post_verify_state_react(
            ctx,
            trace="t2",
            namespace="ns1",
            deployment="nginx",
            verify_summary="",
            dep_detail="",
            stdout="",
            last_attempt=3,  # next=4 > max=3
        )
        assert result is False

    async def test_plan_none_returns_false(self):
        """run_post_verify_react_loop returns None → False."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=r)
        ctx.settings = _make_settings(omni_post_verify_state_llm_enabled=True)
        with patch("workers.autonomous_feedback_loop.run_post_verify_react_loop",
                   new=AsyncMock(return_value=None)):
            result = await _llm_post_verify_state_react(
                ctx,
                trace="t3",
                namespace="ns1",
                deployment="nginx",
                verify_summary="fail",
                dep_detail="not ready",
                stdout="done",
                last_attempt=0,
            )
        assert result is False

    async def test_plan_with_empty_tool_name_returns_false(self):
        """plan returned but tool_name empty → False."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=r)
        ctx.settings = _make_settings(omni_post_verify_state_llm_enabled=True)
        with patch("workers.autonomous_feedback_loop.run_post_verify_react_loop",
                   new=AsyncMock(return_value={"tool_name": "", "args": {}})):
            result = await _llm_post_verify_state_react(
                ctx,
                trace="t4",
                namespace="ns1",
                deployment="nginx",
                verify_summary="",
                dep_detail="",
                stdout="",
                last_attempt=0,
            )
        assert result is False

    async def test_valid_plan_emits_mutate_returns_true(self):
        """Valid plan → writes state + emits mutate → True."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=r)
        ctx.settings = _make_settings(omni_post_verify_state_llm_enabled=True,
                                       autonomous_execute_max_attempts=5)
        emits = []
        valid_plan = {
            "tool_name": "k8s_rollout_restart",
            "args": {"namespace": "ns1", "deployment": "nginx"},
            "reasoning_chain": {"verdict": "RETRY", "lane": "state"},
        }

        async def fake_emit_mutate(ctx, *, trace, tool_name, args, attempt_count, reasoning_chain=None):
            emits.append({"trace": trace, "tool": tool_name})
            return True

        with patch("workers.autonomous_feedback_loop.run_post_verify_react_loop",
                   new=AsyncMock(return_value=valid_plan)), \
             _PatchAfl(emit_execute_mutate=fake_emit_mutate):
            result = await _llm_post_verify_state_react(
                ctx,
                trace="t5",
                namespace="ns1",
                deployment="nginx",
                verify_summary="still failing",
                dep_detail="pods crashlooping",
                stdout="done",
                last_attempt=0,
            )
        assert result is True
        assert len(emits) == 1
        assert emits[0]["tool"] == "k8s_rollout_restart"


# ---------------------------------------------------------------------------
# _llm_replan_after_feedback
# ---------------------------------------------------------------------------


class TestLlmReplanAfterFeedback:
    async def test_llm_exception_returns_none(self):
        """LLM raises → exception caught → None."""
        r = aioredis.FakeRedis(decode_responses=True)
        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(side_effect=RuntimeError("LLM down"))
        ctx = _make_ctx(redis=r)
        ctx.llm = mock_llm
        ctx.settings = _make_settings()

        result = await _llm_replan_after_feedback(ctx, "t1", "out", "err", 1)
        assert result is None

    async def test_parsed_no_tool_name_returns_no_op(self):
        """LLM returns JSON with empty tool_name → {"tool_name": "no_op", "args": {}}."""
        r = aioredis.FakeRedis(decode_responses=True)
        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value={
            "message": {"content": '{"tool_name": "", "args": {}}'}
        })
        ctx = _make_ctx(redis=r)
        ctx.llm = mock_llm
        ctx.settings = _make_settings()

        with patch("workers.autonomous_feedback_loop._parse_tool_json",
                   return_value={"tool_name": "", "args": {}}), \
             patch("workers.autonomous_feedback_loop.log_llm_trace"), \
             patch("workers.autonomous_feedback_loop.agentic_parse_failure_hint", return_value="ok"):
            result = await _llm_replan_after_feedback(ctx, "t2", "out", "err", 1)
        assert result == {"tool_name": "no_op", "args": {}}

    async def test_parsed_valid_k8s_rollout_restart_returned(self):
        """LLM returns valid k8s_rollout_restart plan → returned."""
        r = aioredis.FakeRedis(decode_responses=True)
        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value={
            "message": {"content": '{"tool_name":"k8s_rollout_restart","args":{"namespace":"ns1","deployment":"dep1"}}'}
        })
        ctx = _make_ctx(redis=r)
        ctx.llm = mock_llm
        ctx.settings = _make_settings()

        valid_plan = {"tool_name": "k8s_rollout_restart", "args": {"namespace": "ns1", "deployment": "dep1"}}
        with patch("workers.autonomous_feedback_loop._parse_tool_json", return_value=valid_plan), \
             patch("workers.autonomous_feedback_loop.log_llm_trace"), \
             patch("workers.autonomous_feedback_loop.agentic_parse_failure_hint", return_value="ok"):
            result = await _llm_replan_after_feedback(ctx, "t3", "out", "err", 1)
        assert result is not None
        assert result["tool_name"] == "k8s_rollout_restart"

    async def test_parse_returns_none_returns_none(self):
        """_parse_tool_json returns None → function returns None."""
        r = aioredis.FakeRedis(decode_responses=True)
        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value={"message": {"content": "not json"}})
        ctx = _make_ctx(redis=r)
        ctx.llm = mock_llm
        ctx.settings = _make_settings()

        with patch("workers.autonomous_feedback_loop._parse_tool_json", return_value=None), \
             patch("workers.autonomous_feedback_loop.log_llm_trace"), \
             patch("workers.autonomous_feedback_loop.agentic_parse_failure_hint", return_value="bad"):
            result = await _llm_replan_after_feedback(ctx, "t4", "out", "err", 1)
        assert result is None

    async def test_ctx_blob_extraction_success(self):
        """ctx_blob in redis → snippet extracted."""
        r = aioredis.FakeRedis(decode_responses=True)
        await r.set("omni:autonomous:ctx:t5", json.dumps({"sanitized_text": "CPU spike context"}))
        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value={"message": {"content": "{}"}})
        ctx = _make_ctx(redis=r)
        ctx.llm = mock_llm
        ctx.settings = _make_settings()

        with patch("workers.autonomous_feedback_loop._parse_tool_json", return_value=None), \
             patch("workers.autonomous_feedback_loop.log_llm_trace"), \
             patch("workers.autonomous_feedback_loop.agentic_parse_failure_hint", return_value="bad"):
            result = await _llm_replan_after_feedback(ctx, "t5", "out", "err", 1)
        assert result is None  # parse returned None


# ---------------------------------------------------------------------------
# handle_action_feedback_envelope — additional branches
# ---------------------------------------------------------------------------


class TestHandleActionFeedbackEnvelopeWave2:
    def _body(self, **kw: Any) -> str:
        defaults = {
            "trace_id": "tr-wave2",
            "exit_code": 1,
            "stdout": "fail output",
            "stderr": "error",
            "tool_name": "k8s_rollout_restart",
            "mutate_args": {},
        }
        defaults.update(kw)
        return json.dumps(defaults)

    async def test_replan_no_op_clears_state(self):
        """Replan returns no_op → state deleted, no emit."""
        r = aioredis.FakeRedis(decode_responses=True)
        await r.set("omni:autonomous:state:tr-noop",
                    json.dumps({"last_attempt_count": 0, "feedback_failures": 0,
                                "sdk_verify_round": 0, "state_verify_attempt": 0}))
        deleted = []

        async def fake_replan(ctx, trace, stdout, stderr, exit_code):
            return {"tool_name": "no_op", "args": {}}

        original_delete = r.delete

        async def spy_delete(*keys):
            deleted.extend(keys)
            return await original_delete(*keys)

        r.delete = spy_delete
        emit_calls = []

        with _PatchAfl(
            emit_transition=_async_capture(emit_calls),
            emit_terminal_tombstone=_noop,
            emit_telegram_escalation=_noop,
            _llm_replan_after_feedback=fake_replan,
            _finalize_if_deployment_rollout_healthy_from_stored_ctx=AsyncMock(return_value=False),
        ):
            ctx = _make_ctx(redis=r,
                            settings=_make_settings(
                                autonomous_execute_max_attempts=3,
                                autonomous_verify_max_rounds=3,
                                omni_feedback_full_agentic_planner_enabled=False,
                            ))
            body = {
                "trace_id": "tr-noop",
                "exit_code": 1,
                "stdout": "fail",
                "stderr": "err",
                "tool_name": "k8s_rollout_restart",
                "mutate_args": {},
            }
            await handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})

        # State key should be deleted for no_op
        assert any("tr-noop" in str(k) for k in deleted)

    async def test_replan_empty_emits_tombstone(self):
        """Replan returns None → REPLAN_EMPTY tombstone."""
        r = aioredis.FakeRedis(decode_responses=True)
        tombstones = []

        async def fake_replan(ctx, trace, stdout, stderr, exit_code):
            return None

        with _PatchAfl(
            emit_transition=_noop,
            emit_terminal_tombstone=_async_capture(tombstones),
            emit_telegram_escalation=_noop,
            _llm_replan_after_feedback=fake_replan,
            _finalize_if_deployment_rollout_healthy_from_stored_ctx=AsyncMock(return_value=False),
        ):
            ctx = _make_ctx(redis=r,
                            settings=_make_settings(
                                autonomous_execute_max_attempts=3,
                                autonomous_verify_max_rounds=3,
                                omni_feedback_full_agentic_planner_enabled=False,
                            ))
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

    async def test_attempt_count_exceeded_emits_tombstone(self):
        """next_attempt (last+1) > max → ATTEMPT_COUNT_EXCEEDED tombstone."""
        r = aioredis.FakeRedis(decode_responses=True)
        # feedback_failures=0 so we don't hit MAX_VERIFY_ROUNDS; last_attempt=2 so max_attempts=3
        # feedback_failures < max_verify, last_attempt < max_attempts, but next_attempt > max
        await r.set("omni:autonomous:state:tr-exceed",
                    json.dumps({"last_attempt_count": 2, "feedback_failures": 0,
                                "sdk_verify_round": 0, "state_verify_attempt": 0}))
        tombstones = []

        with _PatchAfl(
            emit_transition=_noop,
            emit_terminal_tombstone=_async_capture(tombstones),
            emit_telegram_escalation=_noop,
            _finalize_if_deployment_rollout_healthy_from_stored_ctx=AsyncMock(return_value=False),
        ):
            ctx = _make_ctx(redis=r,
                            settings=_make_settings(
                                autonomous_execute_max_attempts=2,  # max=2, last=2 → next=2 → hit last>=max branch
                                autonomous_verify_max_rounds=5,
                                omni_feedback_full_agentic_planner_enabled=False,
                            ))
            body = {
                "trace_id": "tr-exceed",
                "exit_code": 1,
                "stdout": "fail",
                "stderr": "err",
                "tool_name": "k8s_rollout_restart",
                "mutate_args": {},
            }
            await handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})

        # Should be either MAX_MUTATE_ATTEMPTS (last >= max) or ATTEMPT_COUNT_EXCEEDED
        codes = {t.get("reason_code") for t in tombstones}
        assert codes & {"MAX_MUTATE_ATTEMPTS", "ATTEMPT_COUNT_EXCEEDED"}

    async def test_success_with_probe_ids_and_no_pmsv(self):
        """exit_code=0, probe_ids and anomaly_event → no-pmsv path → finalize verified."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx_data = {
            "alertname": "HighCPU",
            "verify_probe_ids": ["cpu_probe"],
            "anomaly_event_min": _valid_anomaly_event_min(),
            "symptom_group": "resource_pressure",
        }
        await r.set("omni:autonomous:ctx:tr-probes", json.dumps(ctx_data))
        finalized = []

        async def fake_finalize_verified(ctx, *, trace, body, mutate_args, stdout, sdk_verify_summary, ctx_obj=None):
            finalized.append(trace)
            return True

        async def fake_run_probes(ctx, *, trace, probe_ids, ev):
            return True, "all probes pass", []

        with _PatchAfl(
            emit_transition=_noop,
            emit_terminal_tombstone=_noop,
            emit_telegram_escalation=_noop,
            _finalize_feedback_success_verified=fake_finalize_verified,
            run_verify_probes=fake_run_probes,
            check_deployment_rollout_healthy=AsyncMock(return_value=(True, "ok")),
            resolve_namespace_deployment_for_state_gate=lambda *a, **kw: ("", ""),
        ):
            ctx = _make_ctx(redis=r,
                            settings=_make_settings(
                                autonomous_execute_max_attempts=3,
                                autonomous_verify_max_rounds=3,
                                omni_post_mutate_sdk_verify_enabled=True,
                                omni_post_mutate_verify_planner_enabled=False,
                                omni_verify_delay_sec=0,
                            ))
            body = {
                "trace_id": "tr-probes",
                "exit_code": 0,
                "stdout": "success",
                "stderr": "",
                "tool_name": "k8s_rollout_restart",
                "mutate_args": {},
            }
            await handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})

        assert "tr-probes" in finalized

    async def test_success_verify_fail_and_deterministic_plan(self):
        """exit_code=0, probe_ids, sdk fail → deterministic plan → emit mutate."""
        r = aioredis.FakeRedis(decode_responses=True)
        ctx_data = {
            "alertname": "HighCPU",
            "verify_probe_ids": ["cpu_probe"],
            "anomaly_event_min": _valid_anomaly_event_min(),
            "symptom_group": "resource_pressure",
        }
        await r.set("omni:autonomous:ctx:tr-sdk-fail", json.dumps(ctx_data))
        mutate_emits = []

        async def fake_run_probes(ctx, *, trace, probe_ids, ev):
            return False, "probe failed", []  # sdk fail

        async def fake_emit_mutate(ctx, *, trace, tool_name, args, attempt_count, reasoning_chain=None):
            mutate_emits.append(tool_name)
            return True

        with _PatchAfl(
            emit_transition=_noop,
            emit_terminal_tombstone=_noop,
            emit_telegram_escalation=_noop,
            run_verify_probes=fake_run_probes,
            store_autonomous_trace_context=AsyncMock(),
            format_batch_sanitized_analyst_user_text=lambda b: "text",
            probe_raws_to_batch_for_deterministic=lambda *a, **kw: [],
            deterministic_mutate_plan_from_batch=lambda *a, **kw: {
                "tool_name": "k8s_rollout_restart",
                "args": {"namespace": "default", "deployment": "nginx"},
                "reasoning_chain": None,
            },
            emit_execute_mutate=fake_emit_mutate,
            _finalize_if_deployment_rollout_healthy=AsyncMock(return_value=False),
        ):
            ctx = _make_ctx(redis=r,
                            settings=_make_settings(
                                autonomous_execute_max_attempts=3,
                                autonomous_verify_max_rounds=3,
                                omni_post_mutate_sdk_verify_enabled=True,
                                omni_post_mutate_verify_planner_enabled=False,
                                omni_verify_delay_sec=0,
                                omni_sdk_verify_max_rounds=3,
                                omni_llm_first_autonomy_enabled=False,
                                omni_legacy_deterministic_fallback=False,
                                omni_feedback_full_agentic_planner_enabled=False,
                            ))
            body = {
                "trace_id": "tr-sdk-fail",
                "exit_code": 0,
                "stdout": "done",
                "stderr": "",
                "tool_name": "k8s_rollout_restart",
                "mutate_args": {},
            }
            await handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})

        assert "k8s_rollout_restart" in mutate_emits


# ---------------------------------------------------------------------------
# Helper functions used in tests
# ---------------------------------------------------------------------------

def _append_noop(lst: list, item: Any) -> None:
    lst.append(item)
