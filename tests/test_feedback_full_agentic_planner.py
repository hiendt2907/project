"""Feedback path: optional full agentic planner on fresh probe batch (same trace)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest

from workers.diagnostic_evidence import ProbeRunRaw
from workers.proactive_models import AnomalyEvent


def _make_ctx(
    *,
    feedback_agentic: bool = False,
    sdk_verify: bool = True,
) -> Any:
    ws = SimpleNamespace(
        autonomous_execute_max_attempts=3,
        autonomous_verify_max_rounds=3,
        omni_post_mutate_sdk_verify_enabled=sdk_verify,
        omni_post_mutate_verify_planner_enabled=False,
        omni_sdk_verify_max_rounds=3,
        omni_sdk_verify_initial_delay_sec=0,
        omni_post_verify_deployment_state_enabled=False,
        omni_feedback_full_agentic_planner_enabled=feedback_agentic,
        kafka_topic_audit_agent="omni-audit",
        kafka_topic_action_feedback="omni-action-feedback",
        diag_evidence_llm_model="qwen2.5-coder-3b",
        model_helper="qwen2.5-coder-3b",
    )
    kafka_mock = MagicMock()
    kafka_mock.send_dict = AsyncMock()
    from workers.handler_context import WorkerHandlerContext

    return WorkerHandlerContext(
        settings=ws,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        llm=AsyncMock(),
        vector_store=MagicMock(),
        ledger=MagicMock(),
        semaphore=AsyncMock(),
        telegram=None,
        kafka=kafka_mock,
    )


def _feedback_ok(trace: str, tool: str = "k8s_rollout_restart") -> dict[str, str]:
    body = {
        "trace_id": trace,
        "tool_name": tool,
        "mutate_args": {"namespace": "ns", "deployment": "dep"},
        "exit_code": 0,
        "stdout": "ok",
        "stderr": "",
        "skipped_reason": "",
    }
    return {"trace_id": trace, "data": json.dumps(body)}


@pytest.mark.asyncio
async def test_sdk_verify_fail_full_agentic_not_called_when_flag_off():
    """OMNI_FEEDBACK_FULL_AGENTIC_PLANNER_ENABLED=false → no _emit_agentic_mutate_if_any."""
    trace = "fb-agentic-flag-off"
    ev = AnomalyEvent(
        trace_id=trace,
        canonical_query=json.dumps({"labels": {"namespace": "ns", "alertname": "Test"}}),
        namespace="ns",
        deployment="dep",
    )
    ctx_obj = {
        "sanitized_text": "symptom",
        "symptom_group": "test",
        "verify_probe_ids": ["k8s_describe_resource"],
        "anomaly_event_min": ev.model_dump(),
        "omni_verify_required": True,
    }
    r = _make_ctx(feedback_agentic=False)
    await r.redis.set(f"omni:autonomous:ctx:{trace}", json.dumps(ctx_obj))
    await r.redis.set(
        f"omni:autonomous:state:{trace}",
        json.dumps({"last_attempt_count": 1, "feedback_failures": 0, "sdk_verify_round": 0}),
    )

    raw_fail = ProbeRunRaw(
        probe_name="k8s_describe_resource",
        status="FAILED",
        raw_text="still bad",
    )

    with patch(
        "workers.autonomous_feedback_loop._emit_agentic_mutate_if_any",
        new_callable=AsyncMock,
    ) as m_full:
        m_full.return_value = False
        with patch(
            "workers.autonomous_feedback_loop.run_verify_probes",
            new_callable=AsyncMock,
        ) as m_verify:
            m_verify.return_value = (False, "probe summary", [raw_fail])
            with patch(
                "workers.autonomous_feedback_loop.deterministic_mutate_plan_from_batch",
            ) as m_det:
                m_det.return_value = {
                    "tool_name": "k8s_rollout_restart",
                    "args": {"namespace": "ns", "deployment": "dep"},
                }
                from workers.autonomous_feedback_loop import handle_action_feedback_envelope

                await handle_action_feedback_envelope(r, _feedback_ok(trace))

    m_full.assert_not_called()
    m_det.assert_called_once()
    assert r.kafka.send_dict.await_count >= 1


@pytest.mark.asyncio
async def test_sdk_verify_fail_full_agentic_skips_deterministic_when_emitted():
    """Flag on + _emit_agentic_mutate_if_any True → early return; deterministic not used."""
    trace = "fb-agentic-flag-on"
    ev = AnomalyEvent(
        trace_id=trace,
        canonical_query=json.dumps({"labels": {"namespace": "ns", "alertname": "Test"}}),
        namespace="ns",
        deployment="dep",
    )
    ctx_obj = {
        "sanitized_text": "symptom",
        "symptom_group": "test",
        "verify_probe_ids": ["k8s_describe_resource"],
        "anomaly_event_min": ev.model_dump(),
        "omni_verify_required": True,
    }
    r = _make_ctx(feedback_agentic=True)
    await r.redis.set(f"omni:autonomous:ctx:{trace}", json.dumps(ctx_obj))
    await r.redis.set(
        f"omni:autonomous:state:{trace}",
        json.dumps({"last_attempt_count": 1, "feedback_failures": 0, "sdk_verify_round": 0}),
    )

    raw_fail = ProbeRunRaw(
        probe_name="k8s_describe_resource",
        status="FAILED",
        raw_text="still bad",
    )

    with patch(
        "workers.autonomous_feedback_loop._emit_agentic_mutate_if_any",
        new_callable=AsyncMock,
    ) as m_full:
        m_full.return_value = True
        with patch(
            "workers.autonomous_feedback_loop.run_verify_probes",
            new_callable=AsyncMock,
        ) as m_verify:
            m_verify.return_value = (False, "probe summary", [raw_fail])
            with patch(
                "workers.autonomous_feedback_loop.deterministic_mutate_plan_from_batch",
            ) as m_det:

                def _boom(*_a: Any, **_k: Any) -> None:
                    raise AssertionError("deterministic should not run when full agentic emitted")

                m_det.side_effect = _boom
                from workers.autonomous_feedback_loop import handle_action_feedback_envelope

                await handle_action_feedback_envelope(r, _feedback_ok(trace))

    m_full.assert_awaited_once()
    assert m_full.await_args.kwargs.get("attempt_count") == 2


@pytest.mark.asyncio
async def test_emit_agentic_mutate_if_any_passes_attempt_count_to_emit(monkeypatch: pytest.MonkeyPatch):
    """_emit_agentic_mutate_if_any uses attempt_count in emit_execute_mutate."""
    from workers import evidence_consumer as ec
    from workers.handler_context import WorkerHandlerContext

    captured: dict[str, Any] = {}

    async def _capture_emit(
        ctx: Any,
        *,
        trace: str,
        tool_name: str,
        args: dict[str, Any],
        attempt_count: int = 1,
        reasoning_chain: dict[str, Any] | None = None,
    ) -> bool:
        captured["attempt_count"] = attempt_count
        captured["trace"] = trace
        return True

    monkeypatch.setattr(ec, "emit_execute_mutate", _capture_emit)
    monkeypatch.setattr(ec, "infer_blind_proof_lane_hint", AsyncMock(return_value=None))
    monkeypatch.setattr(ec, "deterministic_mutate_plan_from_batch", lambda *a, **k: None)
    monkeypatch.setattr(ec, "recall_playbook_advisory", AsyncMock(return_value=None))
    monkeypatch.setattr(ec, "run_agentic_mutate_plan", AsyncMock(return_value=None))
    monkeypatch.setattr(ec, "rollout_args_from_evidence_batch", lambda _b: None)

    ws = SimpleNamespace(
        autonomous_agentic_max_steps=2,
        omni_diagnostic_react_enabled=False,
        omni_planner_precondition_gate_enabled=False,
        trace_correlation_ping_enabled=True,
        # Explicit legacy deterministic path (defaults are LLM-first + no legacy fallback).
        omni_llm_first_autonomy_enabled=False,
        omni_legacy_deterministic_fallback=True,
        kafka_topic_actions="omni-actions",
    )
    kafka = MagicMock()
    kafka.send_dict = AsyncMock()
    ctx = WorkerHandlerContext(
        settings=ws,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        llm=MagicMock(),
        vector_store=MagicMock(),
        ledger=MagicMock(),
        semaphore=AsyncMock(),
        telegram=None,
        kafka=kafka,
    )
    batch = [{"probe": "p1", "result": "FAILED", "extracted_fact": {}}]
    out = await ec._emit_agentic_mutate_if_any(
        ctx,
        "tr-attempt",
        batch,
        sanitized_text="txt",
        attempt_count=4,
    )
    assert out is False
    assert captured.get("attempt_count") != 4

    monkeypatch.setattr(
        ec,
        "deterministic_mutate_plan_from_batch",
        lambda *_a, **_k: {
            "tool_name": "k8s_rollout_restart",
            "args": {"namespace": "n", "deployment": "d"},
            "discovery_steps": [],
        },
    )
    monkeypatch.setattr(ec, "_proof_of_fault_gate", AsyncMock(return_value=(True, "", {"proof_lane": "state"})))
    monkeypatch.setattr(
        ec,
        "evaluate_diagnostic_invariants",
        lambda *_a, **_k: (True, "", {}),
    )
    from workers.autonomous_execute import MUTATE_TOOL_ALLOWLIST

    assert "k8s_rollout_restart" in MUTATE_TOOL_ALLOWLIST

    out2 = await ec._emit_agentic_mutate_if_any(
        ctx,
        "tr-attempt-2",
        batch,
        sanitized_text="txt",
        attempt_count=4,
    )
    assert out2 is True
    assert captured.get("attempt_count") == 4
    assert captured.get("trace") == "tr-attempt-2"


@pytest.mark.asyncio
async def test_emit_agentic_chaos_lab_autofix_after_planner_fail_llm_first(monkeypatch: pytest.MonkeyPatch):
    """When LLM-first planner returns None, lab chaos credential autofix can still emit k8s_patch_secret."""
    from workers import evidence_consumer as ec
    from workers.handler_context import WorkerHandlerContext

    captured: dict[str, Any] = {}

    async def _capture_emit(
        ctx: Any,
        *,
        trace: str,
        tool_name: str,
        args: dict[str, Any],
        attempt_count: int = 1,
        reasoning_chain: dict[str, Any] | None = None,
    ) -> bool:
        captured["tool_name"] = tool_name
        captured["args"] = args
        return True

    monkeypatch.setattr(ec, "emit_execute_mutate", _capture_emit)
    monkeypatch.setattr(ec, "infer_blind_proof_lane_hint", AsyncMock(return_value=None))
    monkeypatch.setattr(ec, "deterministic_mutate_plan_from_batch", lambda *a, **k: None)
    monkeypatch.setattr(ec, "recall_playbook_advisory", AsyncMock(return_value=None))
    monkeypatch.setattr(ec, "run_agentic_mutate_plan", AsyncMock(return_value=None))
    monkeypatch.setattr(ec, "_proof_of_fault_gate", AsyncMock(return_value=(True, "", {"proof_lane": "state"})))
    monkeypatch.setattr(
        ec,
        "evaluate_diagnostic_invariants",
        lambda *_a, **_k: (True, "", {}),
    )

    ws = SimpleNamespace(
        autonomous_agentic_max_steps=2,
        omni_diagnostic_react_enabled=False,
        omni_planner_precondition_gate_enabled=False,
        trace_correlation_ping_enabled=False,
        omni_llm_first_autonomy_enabled=True,
        omni_legacy_deterministic_fallback=True,
        omni_unrestricted_tool_execution=True,
        lab_chaos_credential_autofix_enabled=True,
        chaos_pg_app_password="lab-secret-password",
        chaos_pg_secret_name="chaos-pg-secret",
        chaos_pg_password_key="APP_PASSWORD",
        chaos_lab_namespace="multi-agent",
        omni_probe_driven_mutate_tools="k8s_patch_secret",
    )
    ctx = WorkerHandlerContext(
        settings=ws,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        llm=MagicMock(),
        vector_store=MagicMock(),
        ledger=MagicMock(),
        semaphore=AsyncMock(),
        telegram=None,
        kafka=None,
    )
    batch = [
        {
            "raw": "password authentication failed for user chaos_app",
            "alert_hint": "chaos-victim",
        }
    ]
    out = await ec._emit_agentic_mutate_if_any(
        ctx,
        "tr-chaos-autofix",
        batch,
        sanitized_text="credential failure",
    )
    assert out is True
    assert captured.get("tool_name") == "k8s_patch_secret"
    assert captured["args"].get("namespace") == "multi-agent"
    assert captured["args"].get("name") == "chaos-pg-secret"
    assert captured["args"].get("value") == "lab-secret-password"


@pytest.mark.asyncio
async def test_emit_agentic_chaos_lab_vetoes_rollout_on_credential_failure(monkeypatch: pytest.MonkeyPatch):
    """Planner fault rollout_restart is replaced by chaos k8s_patch_secret when evidence shows auth failure."""
    from workers import evidence_consumer as ec
    from workers.handler_context import WorkerHandlerContext

    captured: dict[str, Any] = {}

    async def _capture_emit(
        ctx: Any,
        *,
        trace: str,
        tool_name: str,
        args: dict[str, Any],
        attempt_count: int = 1,
        reasoning_chain: dict[str, Any] | None = None,
    ) -> bool:
        captured["tool_name"] = tool_name
        return True

    monkeypatch.setattr(ec, "emit_execute_mutate", _capture_emit)
    monkeypatch.setattr(ec, "infer_blind_proof_lane_hint", AsyncMock(return_value=None))
    monkeypatch.setattr(ec, "deterministic_mutate_plan_from_batch", lambda *a, **k: None)
    monkeypatch.setattr(ec, "recall_playbook_advisory", AsyncMock(return_value=None))
    monkeypatch.setattr(
        ec,
        "run_agentic_mutate_plan",
        AsyncMock(
            return_value={
                "tool_name": "k8s_rollout_restart",
                "args": {"namespace": "multi-agent", "deployment": "chaos-victim"},
                "discovery_steps": [],
                "reasoning_chain": {"verdict": "EXECUTE_PLAN", "lane": "state", "thought_process": []},
            }
        ),
    )
    monkeypatch.setattr(ec, "_proof_of_fault_gate", AsyncMock(return_value=(True, "", {"proof_lane": "state"})))
    monkeypatch.setattr(
        ec,
        "evaluate_diagnostic_invariants",
        lambda *_a, **_k: (True, "", {}),
    )

    ws = SimpleNamespace(
        autonomous_agentic_max_steps=2,
        omni_diagnostic_react_enabled=False,
        omni_planner_precondition_gate_enabled=False,
        trace_correlation_ping_enabled=False,
        omni_llm_first_autonomy_enabled=True,
        omni_legacy_deterministic_fallback=True,
        omni_unrestricted_tool_execution=True,
        lab_chaos_credential_autofix_enabled=True,
        chaos_pg_app_password="lab-secret-password",
        chaos_pg_secret_name="chaos-pg-secret",
        chaos_pg_password_key="APP_PASSWORD",
        chaos_lab_namespace="multi-agent",
        omni_probe_driven_mutate_tools="k8s_patch_secret",
    )
    ctx = WorkerHandlerContext(
        settings=ws,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        llm=MagicMock(),
        vector_store=MagicMock(),
        ledger=MagicMock(),
        semaphore=AsyncMock(),
        telegram=None,
        kafka=None,
    )
    batch = [
        {
            "raw": "password authentication failed for user chaos_app",
            "alert_hint": "deployment/chaos-victim",
        }
    ]
    out = await ec._emit_agentic_mutate_if_any(
        ctx,
        "tr-veto-rollout",
        batch,
        sanitized_text="crashloop credential",
    )
    assert out is True
    assert captured.get("tool_name") == "k8s_patch_secret"


def _make_ctx_pmsv() -> Any:
    ws = SimpleNamespace(
        autonomous_execute_max_attempts=3,
        autonomous_verify_max_rounds=3,
        omni_post_mutate_sdk_verify_enabled=True,
        omni_post_mutate_verify_planner_enabled=True,
        omni_verify_delay_sec=0,
        omni_state_verify_max_attempts=2,
        omni_sdk_verify_max_rounds=3,
        omni_sdk_verify_initial_delay_sec=0,
        omni_post_verify_deployment_state_enabled=False,
        omni_feedback_full_agentic_planner_enabled=False,
        omni_post_mutate_state_verify_max_steps=4,
        omni_planner_llm_sole_evaluator=False,
        omni_diagnostic_react_enabled=False,
        diag_evidence_llm_model="qwen2.5-coder-3b",
        model_helper="qwen2.5-coder-3b",
        embed_model="nomic-ai/nomic-embed-text-v1.5",
        rag_hot_cache_ttl_sec=3600,
        memory_canonical_strip_pods=True,
        kafka_topic_audit_agent="omni-audit",
        kafka_topic_action_feedback="omni-action-feedback",
    )
    kafka_mock = MagicMock()
    kafka_mock.send_dict = AsyncMock()
    from workers.handler_context import WorkerHandlerContext

    return WorkerHandlerContext(
        settings=ws,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        llm=AsyncMock(),
        vector_store=MagicMock(),
        ledger=MagicMock(),
        semaphore=AsyncMock(),
        telegram=None,
        kafka=kafka_mock,
    )


@pytest.mark.asyncio
async def test_pmsv_planner_phase_done_triggers_verified_success():
    """OMNI_POST_MUTATE_VERIFY_PLANNER_ENABLED: phase done + probes pass → hot cache + experience upsert."""
    from rag.pgvector_store import EMBED_DIM

    trace = "pmsv-phase-done"
    ev = AnomalyEvent(
        trace_id=trace,
        canonical_query=json.dumps({"labels": {"namespace": "ns", "alertname": "Test"}}),
        namespace="ns",
        deployment="dep",
    )
    ctx_obj = {
        "sanitized_text": "symptom",
        "symptom_group": "test",
        "verify_probe_ids": ["k8s_describe_resource"],
        "anomaly_event_min": ev.model_dump(),
        "omni_verify_required": True,
    }
    r = _make_ctx_pmsv()
    await r.redis.set(f"omni:autonomous:ctx:{trace}", json.dumps(ctx_obj))
    fixed = [0.1] * EMBED_DIM
    r.llm.embed = AsyncMock(return_value={"embeddings": [fixed]})
    r.vector_store.upsert = AsyncMock()

    raw_ok = ProbeRunRaw(
        probe_name="k8s_describe_resource",
        status="PASSED",
        raw_text="ok",
    )

    with patch(
        "workers.autonomous_feedback_loop.run_verify_probes",
        new_callable=AsyncMock,
    ) as m_v:
        m_v.return_value = (True, "probe ok", [raw_ok])
        with patch(
            "workers.autonomous_feedback_loop.run_post_mutate_state_verify_planner",
            new_callable=AsyncMock,
        ) as m_plan:
            m_plan.return_value = {
                "phase": "done",
                "tool_name": "",
                "args": {},
                "resolution_summary": "Fresh facts show recovery vs initial symptom.",
            }
            with patch(
                "workers.autonomous_feedback_loop.check_deployment_rollout_healthy",
                new_callable=AsyncMock,
            ) as m_dep:
                m_dep.return_value = (True, "ready_replicas=1 desired=1")
                from workers.autonomous_feedback_loop import handle_action_feedback_envelope

                await handle_action_feedback_envelope(r, _feedback_ok(trace))

    m_plan.assert_awaited_once()
    assert r.vector_store.upsert.await_count >= 1
    hot = await r.redis.get(f"omni:autonomous:hot:{trace}")
    assert hot is not None
    assert json.loads(hot).get("closed") is True


@pytest.mark.asyncio
async def test_llm_first_mode_disables_deterministic_fallback(monkeypatch: pytest.MonkeyPatch):
    """LLM-first + legacy fallback off: deterministic planner must not run."""
    from workers import evidence_consumer as ec
    from workers.handler_context import WorkerHandlerContext

    ws = SimpleNamespace(
        autonomous_agentic_max_steps=2,
        omni_llm_first_autonomy_enabled=True,
        omni_legacy_deterministic_fallback=False,
        omni_unrestricted_tool_execution=False,
        omni_planner_precondition_gate_enabled=True,
        trace_correlation_ping_enabled=True,
        kafka_topic_actions="omni-actions",
        autonomous_allowed_namespaces="multi-agent",
    )
    kafka = MagicMock()
    kafka.send_dict = AsyncMock()
    ctx = WorkerHandlerContext(
        settings=ws,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        llm=MagicMock(),
        vector_store=MagicMock(),
        ledger=MagicMock(),
        semaphore=AsyncMock(),
        telegram=None,
        kafka=kafka,
    )
    batch = [{"probe": "p1", "result": "FAILED", "extracted_fact": {}, "alert_hint": "fault"}]

    monkeypatch.setattr(ec, "infer_blind_proof_lane_hint", AsyncMock(return_value=None))
    monkeypatch.setattr(ec, "run_agentic_mutate_plan", AsyncMock(return_value=None))

    def _det_forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("deterministic fallback must be disabled in llm-first mode")

    monkeypatch.setattr(ec, "deterministic_mutate_plan_from_batch", _det_forbidden)
    monkeypatch.setattr(ec, "recall_playbook_advisory", AsyncMock(return_value=None))

    out = await ec._emit_agentic_mutate_if_any(
        ctx,
        "tr-llm-first-off-det",
        batch,
        sanitized_text="symptom text",
    )
    assert out is False
    assert kafka.send_dict.await_count >= 1


@pytest.mark.asyncio
async def test_precondition_gate_reasks_planner_before_mutate(monkeypatch: pytest.MonkeyPatch):
    """Missing mutate prerequisites should trigger planner re-ask and then execute."""
    from workers import evidence_consumer as ec
    from workers.handler_context import WorkerHandlerContext

    ws = SimpleNamespace(
        autonomous_agentic_max_steps=3,
        omni_llm_first_autonomy_enabled=True,
        omni_legacy_deterministic_fallback=False,
        omni_unrestricted_tool_execution=False,
        omni_planner_precondition_gate_enabled=True,
        trace_correlation_ping_enabled=True,
        kafka_topic_actions="omni-actions",
        autonomous_allowed_namespaces="multi-agent",
    )
    kafka = MagicMock()
    kafka.send_dict = AsyncMock()
    ctx = WorkerHandlerContext(
        settings=ws,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        llm=MagicMock(),
        vector_store=MagicMock(),
        ledger=MagicMock(),
        semaphore=AsyncMock(),
        telegram=None,
        kafka=kafka,
    )
    batch = [{"probe": "p1", "result": "FAILED", "extracted_fact": {}, "alert_hint": "fault"}]

    calls = {"emit": 0}

    async def _capture_emit(
        _ctx: Any,
        *,
        trace: str,
        tool_name: str,
        args: dict[str, Any],
        attempt_count: int = 1,
        reasoning_chain: dict[str, Any] | None = None,
    ) -> bool:
        calls["emit"] += 1
        calls["tool"] = tool_name
        calls["args"] = dict(args)
        calls["attempt"] = attempt_count
        return True

    seq = [
        {
            "decision": "mutate",
            "tool_name": "k8s_patch_secret",
            "args": {"namespace": "multi-agent", "name": "chaos-pg-secret", "value": "x"},
            "discovery_steps": [],
            "missing_preconditions": ["secret_key_unknown"],
        },
        {
            "decision": "mutate",
            "tool_name": "k8s_patch_secret",
            "args": {
                "namespace": "multi-agent",
                "name": "chaos-pg-secret",
                "key": "APP_PASSWORD",
                "value": "x",
                "value_source": "lab_env",
                "value_source_ref": "chaos-baseline",
            },
            "discovery_steps": ["k8s_get_pod_secret_refs", "k8s_get_secret_keys"],
        },
    ]

    async def _next_plan(*_a: Any, **_k: Any) -> dict[str, Any] | None:
        return seq.pop(0) if seq else None

    monkeypatch.setattr(ec, "infer_blind_proof_lane_hint", AsyncMock(return_value=None))
    monkeypatch.setattr(ec, "run_agentic_mutate_plan", _next_plan)
    monkeypatch.setattr(ec, "recall_playbook_advisory", AsyncMock(return_value=None))
    monkeypatch.setattr(ec, "_proof_of_fault_gate", AsyncMock(return_value=(True, "", {"proof_lane": "state"})))
    monkeypatch.setattr(ec, "evaluate_diagnostic_invariants", lambda *_a, **_k: (True, "", {}))
    monkeypatch.setattr(ec, "emit_execute_mutate", _capture_emit)

    out = await ec._emit_agentic_mutate_if_any(
        ctx,
        "tr-precondition-reask",
        batch,
        sanitized_text="symptom text",
        attempt_count=2,
    )
    assert out is True
    assert calls["emit"] == 1
    assert calls["tool"] == "k8s_patch_secret"
    assert calls["args"].get("key") == "APP_PASSWORD"
    assert calls["attempt"] == 2


def _make_ctx_telegram_suppress() -> Any:
    from workers.handler_context import WorkerHandlerContext
    from rag.pgvector_store import EMBED_DIM

    ws = SimpleNamespace(
        autonomous_execute_max_attempts=3,
        autonomous_verify_max_rounds=3,
        omni_post_mutate_sdk_verify_enabled=True,
        omni_post_mutate_verify_planner_enabled=False,
        omni_sdk_verify_max_rounds=3,
        omni_sdk_verify_initial_delay_sec=0,
        omni_verify_delay_sec=0,
        omni_post_verify_deployment_state_enabled=True,
        omni_telegram_suppress_when_deployment_healthy=True,
        omni_llm_first_autonomy_enabled=True,
        omni_legacy_deterministic_fallback=False,
        omni_feedback_full_agentic_planner_enabled=False,
        embed_model="nomic-embed-text",
        kafka_topic_audit_agent="omni-audit",
        kafka_topic_action_feedback="omni-action-feedback",
        diag_evidence_llm_model="qwen2.5-coder-3b",
        model_helper="qwen2.5-coder-3b",
    )
    kafka_mock = MagicMock()
    kafka_mock.send_dict = AsyncMock()
    r = WorkerHandlerContext(
        settings=ws,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        llm=AsyncMock(),
        vector_store=MagicMock(),
        ledger=MagicMock(),
        semaphore=AsyncMock(),
        telegram=None,
        kafka=kafka_mock,
    )
    fixed = [0.1] * EMBED_DIM
    r.llm.embed = AsyncMock(return_value={"embeddings": [fixed]})
    r.vector_store.upsert = AsyncMock()
    return r


@pytest.mark.asyncio
async def test_sdk_verify_no_agentic_suppresses_telegram_when_rollout_healthy():
    """Deployment rollout healthy → finalize success; no Telegram on SDK_VERIFY_NO_AGENTIC_PLAN."""
    from workers.autonomy_contract import TRANSITION_STATE_MACHINE_VERIFIED

    trace = "fb-suppress-telegram-healthy"
    ev = AnomalyEvent(
        trace_id=trace,
        canonical_query=json.dumps({"labels": {"namespace": "ns", "alertname": "Test"}}),
        namespace="ns",
        deployment="dep",
    )
    ctx_obj = {
        "sanitized_text": "symptom",
        "symptom_group": "test",
        "verify_probe_ids": ["k8s_describe_resource"],
        "anomaly_event_min": ev.model_dump(),
        "omni_verify_required": True,
    }
    r = _make_ctx_telegram_suppress()
    await r.redis.set(f"omni:autonomous:ctx:{trace}", json.dumps(ctx_obj))
    await r.redis.set(
        f"omni:autonomous:state:{trace}",
        json.dumps({"last_attempt_count": 1, "feedback_failures": 0, "sdk_verify_round": 0}),
    )

    raw_fail = ProbeRunRaw(
        probe_name="k8s_describe_resource",
        status="FAILED",
        raw_text="stale pod name in promql",
    )

    with patch(
        "workers.autonomous_feedback_loop._emit_agentic_mutate_if_any",
        new_callable=AsyncMock,
    ) as m_emit_agentic:
        m_emit_agentic.return_value = False
        with patch(
            "workers.autonomous_feedback_loop.run_verify_probes",
            new_callable=AsyncMock,
        ) as m_verify:
            m_verify.return_value = (False, "verify stale pod", [raw_fail])
            with patch(
                "workers.autonomous_feedback_loop.check_deployment_rollout_healthy",
                new_callable=AsyncMock,
            ) as m_dep:
                m_dep.return_value = (True, "ready_replicas=1 desired=1")
                with patch(
                    "workers.autonomous_feedback_loop.emit_telegram_escalation",
                    new_callable=AsyncMock,
                ) as m_tg:
                    from workers.autonomous_feedback_loop import handle_action_feedback_envelope

                    await handle_action_feedback_envelope(r, _feedback_ok(trace))

    m_tg.assert_not_awaited()
    audited = [c.args[1] for c in r.kafka.send_dict.await_args_list if c.args]
    assert any(
        TRANSITION_STATE_MACHINE_VERIFIED in (json.loads(x.get("data") or "{}") or {}).get("transition", "")
        for x in audited
        if isinstance(x, dict)
    )
    hot = await r.redis.get(f"omni:autonomous:hot:{trace}")
    assert hot is not None
    assert json.loads(hot).get("closed") is True
