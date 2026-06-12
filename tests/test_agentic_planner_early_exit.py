"""Early exit when batch classifies as credential failure (no redundant ReAct discovery)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from tests.llm_mock_compat import CompatLLM

from pkg.reasoning.reason_codes import PLANNER_PHASE_DONE


@pytest.mark.asyncio
async def test_run_agentic_mutate_plan_react_off_includes_readonly_observations_in_next_llm_turn(
    monkeypatch,
):
    """OMNI_DIAGNOSTIC_REACT_ENABLED=false must still append discovery output to the user prompt."""
    from workers import analyst_agentic_loop as aal

    async def fake_readonly(ctx, tool_name, args, **kwargs):
        return "DESCRIBE_SNIPPET_FOR_TEST"

    monkeypatch.setattr(aal, "_execute_readonly_tool", fake_readonly)

    batch = [
        {
            "probe": "k8s_clinical_pod_status",
            "result": "PASSED",
            "raw": "phase=Running",
            "extracted_fact": {},
        }
    ]
    llm = CompatLLM()
    llm.chat = AsyncMock(
        side_effect=[
            {
                "message": {
                    "content": (
                        '{"tool_name":"k8s_describe_resource",'
                        '"args":{"resource_type":"Pod","name":"p","namespace":"multi-agent"},'
                        '"step":"readonly"}'
                    )
                }
            },
            {
                "message": {
                    "content": '{"phase":"done","analysis":"verified","tool_name":"","args":{},"step":"mutate"}'
                }
            },
        ]
    )
    stored: dict[str, str] = {}

    async def fake_get(key: str) -> str | None:
        return stored.get(key)

    async def fake_setex(key: str, _ttl: int, raw: str) -> None:
        stored[key] = raw if isinstance(raw, str) else str(raw)

    redis = AsyncMock()
    redis.get = fake_get
    redis.setex = fake_setex
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            diag_evidence_llm_model="m1",
            model_reasoning_engine="",
            model_helper="",
            chat_model="",
            omni_diagnostic_react_enabled=False,
            omni_diagnostic_react_readonly_max=3,
            autonomous_agentic_max_steps=4,
        ),
        llm=llm,
        redis=redis,
    )
    out = await aal.run_agentic_mutate_plan(
        ctx,
        trace="tr-obs",
        sanitized_text="symptom",
        batch=batch,
        max_steps=4,
    )
    assert out is not None
    assert llm.chat.await_count == 2
    second_user = llm.chat.call_args_list[1].kwargs["messages"][1]["content"]
    assert "<TRACE_MEMORY>" in second_user
    assert "<HISTORY>" in second_user
    assert "DESCRIBE_SNIPPET_FOR_TEST" in second_user
    assert "k8s_describe_resource" in second_user


@pytest.mark.asyncio
async def test_run_agentic_mutate_plan_calls_llm_on_credential_batch_no_early_exit():
    """Credential failure no longer short-circuits the planner — LLM may propose k8s_patch_secret."""
    from workers import analyst_agentic_loop as aal

    batch = [
        {
            "probe": "k8s_clinical_pod_log_previous",
            "result": "PASSED",
            "raw": "FATAL: password authentication failed for user chaos_app",
            "extracted_fact": {"status": "PASSED"},
        }
    ]
    llm = CompatLLM()
    llm.chat = AsyncMock(
        return_value={
            "message": {
                "content": (
                    '{"thought":"auth failure","tool_name":"k8s_patch_secret",'
                    '"args":{"namespace":"multi-agent","name":"chaos-pg-secret","key":"APP_PASSWORD",'
                        '"value":"x","reasoning":"restore"},'
                        '"evidence_refs":["fact:credential_failure"],"phase":"remediate","step":"mutate"}'
                )
            }
        }
    )
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            diag_evidence_llm_model="m1",
            model_reasoning_engine="",
            model_helper="",
            chat_model="",
            autonomous_agentic_max_steps=2,
        ),
        llm=llm,
        redis=redis,
    )
    out = await aal.run_agentic_mutate_plan(
        ctx,
        trace="tr-cred",
        sanitized_text="symptom",
        batch=batch,
        max_steps=2,
    )
    assert out is not None
    assert out.get("tool_name") == "k8s_patch_secret"
    llm.chat.assert_called()


@pytest.mark.asyncio
async def test_run_agentic_mutate_plan_still_calls_llm_without_credential_signal():
    from workers import analyst_agentic_loop as aal

    batch = [
        {
            "probe": "k8s_clinical_pod_status",
            "result": "PASSED",
            "raw": "phase=Running",
            "extracted_fact": {"has_crash_loop": True},
        }
    ]
    llm = CompatLLM()
    llm.chat = AsyncMock(
        return_value={
            "message": {
                "content": '{"phase":"done","analysis":"x","tool_name":"","args":{},"step":"mutate"}'
            }
        }
    )
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            diag_evidence_llm_model="m1",
            model_reasoning_engine="",
            model_helper="",
            chat_model="",
            omni_diagnostic_react_enabled=False,
            autonomous_agentic_max_steps=2,
        ),
        llm=llm,
    )
    out = await aal.run_agentic_mutate_plan(
        ctx,
        trace="tr-norm",
        sanitized_text="symptom",
        batch=batch,
        max_steps=2,
    )
    assert out is not None
    assert llm.chat.await_count >= 1


def test_planner_phase_done_diagnosis_prefers_resolution_and_merges_when_distinct():
    from workers.evidence_consumer import _planner_phase_done_diagnosis

    assert _planner_phase_done_diagnosis("final line", "resolution line") == (
        "resolution line\n\nfinal line"
    )
    assert _planner_phase_done_diagnosis("same", "same") == "same"
    assert _planner_phase_done_diagnosis("", "only_rs") == "only_rs"
    assert _planner_phase_done_diagnosis("only_fa", "") == "only_fa"


@pytest.mark.asyncio
async def test_run_agentic_mutate_plan_done_with_resolution_summary_updates_memory():
    from workers import analyst_agentic_loop as aal

    batch = [{"probe": "k8s_clinical_pod_status", "result": "PASSED", "raw": "", "extracted_fact": {}}]
    llm = CompatLLM()
    llm.chat = AsyncMock(
        return_value={
            "message": {
                "content": (
                    '{"phase":"done","thought":"verified read-only","resolution_summary":'
                    '"describe shows Ready replicas=1 vs prior CrashLoop; logs clean.",'
                    '"tool_name":"","args":{},"step":"mutate"}'
                )
            }
        }
    )
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            diag_evidence_llm_model="m1",
            model_reasoning_engine="",
            model_helper="",
            chat_model="",
            omni_diagnostic_react_enabled=False,
            autonomous_agentic_max_steps=2,
        ),
        llm=llm,
        redis=redis,
    )
    out = await aal.run_agentic_mutate_plan(
        ctx,
        trace="tr-res-sum",
        sanitized_text="CrashLoopBackOff",
        batch=batch,
        max_steps=2,
    )
    assert out is not None
    assert out.get("reason_code") == PLANNER_PHASE_DONE
    assert out.get("final_analysis") == (
        "describe shows Ready replicas=1 vs prior CrashLoop; logs clean."
    )
    assert out.get("resolution_summary") == (
        "describe shows Ready replicas=1 vs prior CrashLoop; logs clean."
    )
    rc = out.get("reasoning_chain") or {}
    assert rc.get("resolution_summary") == (
        "describe shows Ready replicas=1 vs prior CrashLoop; logs clean."
    )
    assert redis.setex.await_count >= 1
    payload = json.loads(redis.setex.call_args_list[-1][0][2])
    assert payload.get("working_hypothesis", "").startswith("Resolved:")


@pytest.mark.asyncio
async def test_run_agentic_mutate_plan_rejects_done_with_tool_until_steps_exhausted():
    from workers import analyst_agentic_loop as aal

    batch = [{"probe": "x", "result": "PASSED", "raw": "", "extracted_fact": {}}]
    llm = CompatLLM()
    llm.chat = AsyncMock(
        return_value={
            "message": {
                "content": (
                    '{"phase":"done","thought":"bad","tool_name":"k8s_rollout_restart",'
                    '"args":{"namespace":"n","deployment":"d"},"step":"mutate"}'
                )
            }
        }
    )
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            diag_evidence_llm_model="m1",
            model_reasoning_engine="",
            model_helper="",
            chat_model="",
            autonomous_agentic_max_steps=1,
        ),
        llm=llm,
        redis=redis,
    )
    out = await aal.run_agentic_mutate_plan(
        ctx,
        trace="tr-reject-done-tool",
        sanitized_text="symptom",
        batch=batch,
        max_steps=1,
    )
    assert out is None
    llm.chat.assert_awaited()


@pytest.mark.asyncio
async def test_run_agentic_mutate_plan_loop_guard_blocks_repeated_readonly(monkeypatch):
    from workers import analyst_agentic_loop as aal

    readonly_calls: list[tuple[str, dict[str, str]]] = []

    async def fake_readonly(ctx, tool_name, args, **kwargs):
        readonly_calls.append((tool_name, dict(args)))
        return "[DATA] ok\n[DIAGNOSIS] same output"

    monkey_llm = CompatLLM()
    monkey_llm.chat = AsyncMock(
        return_value={
            "message": {
                "content": (
                    '{"decision":"discovery","tool_name":"k8s_get_logs",'
                    '"args":{"namespace":"multi-agent","pod":"chaos-victim-0"},'
                    '"step":"readonly"}'
                )
            }
        }
    )
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            diag_evidence_llm_model="m1",
            model_reasoning_engine="",
            model_helper="",
            chat_model="",
            omni_diagnostic_react_enabled=True,
            omni_diagnostic_react_readonly_max=6,
            autonomous_agentic_max_steps=4,
        ),
        llm=monkey_llm,
        redis=redis,
    )
    monkeypatch.setattr(aal, "_execute_readonly_tool", fake_readonly)
    out = await aal.run_agentic_mutate_plan(
        ctx,
        trace="tr-loop-guard",
        sanitized_text="symptom",
        batch=[{"probe": "p", "result": "FAILED", "raw": "", "extracted_fact": {}}],
        max_steps=4,
    )

    assert out is None
    assert len(readonly_calls) == 1
    assert monkey_llm.chat.await_count >= 2


@pytest.mark.asyncio
async def test_planner_missing_preconditions_requires_secret_provenance_and_normalizes_stale_labels():
    from workers import evidence_consumer as ec

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(omni_planner_precondition_gate_enabled=True),
        redis=redis,
    )
    missing = await ec._planner_missing_preconditions(
        ctx,
        trace="tr-precond-secret",
        tool_name="k8s_patch_secret",
        args={
            "namespace": "multi-agent",
            "name": "chaos-pg-secret",
            "key": "APP_PASSWORD",
            "value": "x",
        },
        discovery_steps=["k8s_get_pod_secret_refs", "k8s_get_secret_keys"],
        planner_missing=["arg:namespace", "readonly_discovery_evidence", "evidence:secret_ref_confirmed"],
    )
    assert "arg:namespace" not in missing
    assert "readonly_discovery_evidence" not in missing
    assert "evidence:secret_ref_confirmed" not in missing
    assert "evidence:credential_source_of_truth" in missing


@pytest.mark.asyncio
async def test_planner_missing_preconditions_honors_default_required_fields():
    from workers import evidence_consumer as ec

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(omni_planner_precondition_gate_enabled=True),
        redis=redis,
    )
    missing = await ec._planner_missing_preconditions(
        ctx,
        trace="tr-precond-defaults",
        tool_name="k8s_apply_rbac_least_privilege",
        args={},
        discovery_steps=["k8s_describe_resource"],
        planner_missing=[],
    )
    assert "arg:executor_sa" not in missing
    assert "arg:namespace" not in missing


@pytest.mark.asyncio
async def test_run_agentic_mutate_plan_accepts_tool_args_for_mutate():
    from workers import analyst_agentic_loop as aal

    llm = CompatLLM()
    llm.chat = AsyncMock(
        return_value={
            "message": {
                "content": (
                    '{"decision":"mutate","tool_name":"k8s_patch_secret",'
                    '"tool_args":{"namespace":"multi-agent","name":"sec","key":"DB_PASSWORD","value":"x",'
                    '"value_source":"lab_env","value_source_ref":"ticket-1"},'
                    '"evidence_refs":["history:secret_ref"],'
                    '"step":"mutate"}'
                )
            }
        }
    )
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            diag_evidence_llm_model="m1",
            model_reasoning_engine="",
            model_helper="",
            chat_model="",
            autonomous_agentic_max_steps=2,
        ),
        llm=llm,
        redis=redis,
    )
    out = await aal.run_agentic_mutate_plan(
        ctx,
        trace="tr-tool-args",
        sanitized_text="symptom",
        batch=[{"probe": "p", "result": "FAILED", "raw": "", "extracted_fact": {}}],
        max_steps=2,
    )
    assert out is not None
    assert out.get("tool_name") == "k8s_patch_secret"
    assert out.get("args", {}).get("key") == "DB_PASSWORD"


def test_os_command_item_empty_evidence_refs_raises():
    """Line 47: empty evidence_refs raises ValueError in OSCommandItem validator."""
    from workers.schemas.agentic_planner import OSCommandItem
    import pytest
    with pytest.raises(Exception):
        OSCommandItem(
            purpose="test step",
            dry_run_command="df -h",
            command="df -h",
            target="/",
            expected_output="usage",
            rollback_command="no rollback",
            evidence_refs=["   "],  # blank strings → normalizes to [] → raises
        )


@pytest.mark.asyncio
async def test_schema_reject_recorded_in_trace_memory_so_next_round_reacts():
    """A rejected (no tool_name) round must be appended to TRACE_MEMORY so the NEXT
    prompt shows the failed attempt — the loop reacts instead of repeating verbatim.

    Regression: previously the reject branch `continue`d without recording, so every
    round received an identical prompt (trace_history_actions=0) and the planner
    re-emitted the identical invalid output until the streak-abort backstop fired.
    """
    from workers import analyst_agentic_loop as aal

    batch = [
        {
            "probe": "node_cpu_saturation",
            "result": "PASSED",
            "raw": "KubePodNotReady pod=nginx-test not Ready",
            "extracted_fact": {"status": "PASSED"},
        }
    ]
    # qwen's bare dialect: valid JSON but NO tool_name/decision → ERR_REA_SCHEMA_VIOLATION.
    bare = {"message": {"content": '{"resource_type":"Pod","name":"nginx-test","namespace":"multi-agent"}'}}
    llm = CompatLLM()
    llm.chat = AsyncMock(side_effect=[bare, bare, bare])

    stored: dict[str, str] = {}

    async def fake_get(key: str) -> str | None:
        return stored.get(key)

    async def fake_setex(key: str, _ttl: int, raw: str) -> None:
        stored[key] = raw if isinstance(raw, str) else str(raw)

    redis = AsyncMock()
    redis.get = fake_get
    redis.setex = fake_setex
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            diag_evidence_llm_model="m1",
            model_reasoning_engine="",
            model_helper="",
            chat_model="",
            omni_diagnostic_react_enabled=True,
            omni_diagnostic_react_readonly_max=3,
            autonomous_agentic_max_steps=5,
        ),
        llm=llm,
        redis=redis,
    )
    out = await aal.run_agentic_mutate_plan(
        ctx,
        trace="tr-reject-react",
        sanitized_text="symptom",
        batch=batch,
        max_steps=5,
    )
    assert out is not None
    # Round 2 must have seen the round-1 rejection recorded in TRACE_MEMORY.
    assert llm.chat.await_count >= 2
    second_user = llm.chat.call_args_list[1].kwargs["messages"][1]["content"]
    assert "REJECTED" in second_user
    assert "schema_reject" in second_user
