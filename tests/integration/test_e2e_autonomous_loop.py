"""
E2E integration: Omni agentic planner + simulated cluster (glassbox audit).

Architecture note (split topology): ``run_agentic_mutate_plan`` returns immediately when the
LLM emits a valid **mutate** JSON — mutate is **not** executed in-process; omni-executor does.
Therefore a full diagnose → patch → verify → ``phase: done`` story requires **two** planner
invocations: (A) mutate planned + ``SimulatedClusterState.apply_patch_secret``, (B) readonly
verify then evidence-based termination.

This module uses a **prompt-driven** ``SmartLLMStub`` (no ``AsyncMock(side_effect=[...])`` lists).
Decisions are indexed by ``invocation_id`` + ``round_index`` and recorded with ``decision_rule``
for audit; user prompts are hashed into the trail.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
import pytest

from pkg.reasoning.reason_codes import PLANNER_PHASE_DONE
from workers import analyst_agentic_loop as aal
from workers.memory.trace_memory import load_trace_memory

from .e2e_sim_cluster import SimulatedClusterState
from .react_audit import ReActAuditTrail, assert_audit_sequence_kinds


class SmartLLMStub:
    """
    Stateful stub: each ``chat()`` advances a small state machine (phase + round_index).
    Not a fixed tuple of responses — coupled to invocation id and round, with audit rationale.
    """

    def __init__(self, trail: ReActAuditTrail, sim: SimulatedClusterState) -> None:
        self.trail = trail
        self.sim = sim
        self.invocation_id = "A"
        self._round_idx = 0

    def reset(self, invocation_id: str) -> None:
        self.invocation_id = invocation_id
        self._round_idx = 0

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        format: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        user_content = str(messages[-1].get("content") or "")
        rid = self.invocation_id
        ri = self._round_idx
        self._round_idx += 1

        if rid == "A":
            if ri == 0:
                raw = json.dumps(
                    {
                        "decision": "discovery",
                        "thought": "Describe Secret referenced by workload to confirm credential source.",
                        "tool_name": "k8s_describe_resource",
                        "args": {
                            "resource_type": "Secret",
                            "name": self.sim.secret_name,
                            "namespace": self.sim.namespace,
                        },
                        "phase": "verify",
                        "step": "readonly",
                    },
                    ensure_ascii=False,
                )
                self.trail.record_llm(
                    invocation_id=rid,
                    round_index=ri,
                    user_content=user_content,
                    llm_raw=raw,
                    decision_rule="A_credential_first_readonly",
                    parsed_summary="k8s_describe_resource Secret",
                )
                return {"message": {"content": raw}}
            if ri == 1:
                raw = json.dumps(
                    {
                        "decision": "mutate",
                        "thought": "Patch Secret key to restore valid credential for DB.",
                        "tool_name": "k8s_patch_secret",
                        "args": {
                            "namespace": self.sim.namespace,
                            "name": self.sim.secret_name,
                            "key": self.sim.secret_key,
                            "value": "e2e-restored-credential",
                            "reasoning": "Simulated E2E: align Secret with DB password.",
                        },
                        "phase": "remediate",
                        "step": "mutate",
                        "evidence_refs": ["trace:e2e-react-glassbox-trace"],
                        "explain": "Patching secret to match DB password.",
                        "advise": "Verify pod comes up.",
                    },
                    ensure_ascii=False,
                )
                self.trail.record_llm(
                    invocation_id=rid,
                    round_index=ri,
                    user_content=user_content,
                    llm_raw=raw,
                    decision_rule="A_emit_patch_secret_after_readonly",
                    parsed_summary="k8s_patch_secret",
                )
                return {"message": {"content": raw}}
            raise AssertionError(f"unexpected LLM round in phase A: {ri}")

        if rid == "B":
            if "mutate_planned" not in user_content and "k8s_patch_secret" not in user_content:
                raise AssertionError("phase B prompt should include prior mutate_planned / patch_secret in HISTORY")
            if ri == 0:
                raw = json.dumps(
                    {
                        "decision": "discovery",
                        "thought": "Re-describe Secret after executor applied patch.",
                        "tool_name": "k8s_describe_resource",
                        "args": {
                            "resource_type": "Secret",
                            "name": self.sim.secret_name,
                            "namespace": self.sim.namespace,
                        },
                        "phase": "verify",
                        "step": "readonly",
                    },
                    ensure_ascii=False,
                )
                self.trail.record_llm(
                    invocation_id=rid,
                    round_index=ri,
                    user_content=user_content,
                    llm_raw=raw,
                    decision_rule="B_verify_readonly_post_executor",
                    parsed_summary="k8s_describe_resource Secret",
                )
                return {"message": {"content": raw}}
            if ri == 1:
                raw = json.dumps(
                    {
                        "decision": "discovery",
                        "thought": "Read-only output shows healthy sync; exit.",
                        "phase": "done",
                        "resolution_summary": "Secret describe shows healthy rotation and DB auth success vs initial credential mismatch.",
                        "tool_name": "",
                        "args": {},
                        "step": "readonly",
                    },
                    ensure_ascii=False,
                )
                self.trail.record_llm(
                    invocation_id=rid,
                    round_index=ri,
                    user_content=user_content,
                    llm_raw=raw,
                    decision_rule="B_phase_done_after_verify",
                    parsed_summary="phase_done",
                )
                return {"message": {"content": raw}}
            raise AssertionError(f"unexpected LLM round in phase B: {ri}")

        raise AssertionError(f"unknown invocation_id={rid!r}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_e2e_react_credential_secret_patch_two_phase_glassbox(monkeypatch: pytest.MonkeyPatch) -> None:
    trail = ReActAuditTrail()
    sim = SimulatedClusterState()
    stub = SmartLLMStub(trail, sim)

    async def _patched_readonly(ctx: Any, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        obs = sim.execute_readonly(tool_name, dict(args or {}))
        trail.record_readonly(
            invocation_id=stub.invocation_id,
            tool_name=tool_name,
            args=dict(args or {}),
            observation=obs,
            simulator_state=sim.snapshot(),
        )
        return obs

    monkeypatch.setattr(aal, "_execute_readonly_tool", _patched_readonly)

    trace = "e2e-react-glassbox-trace"
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    batch = [
        {
            "probe": "k8s_clinical_pod_log_previous",
            "result": "PASSED",
            "raw": "FATAL: password authentication failed for user chaos_app",
            "extracted_fact": {"namespace": "multi-agent"},
        }
    ]
    sanitized = "CrashLoop: DB password authentication failed (credential drift)."

    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            diag_evidence_llm_model="stub-model",
            model_reasoning_engine="",
            model_helper="",
            chat_model="",
            omni_diagnostic_react_enabled=True,
            omni_diagnostic_react_readonly_max=6,
            autonomous_agentic_max_steps=8,
            tool_output_max_chars=2000,
        ),
        llm=stub,
        redis=redis,
    )

    stub.reset("A")
    out_a = await aal.run_agentic_mutate_plan(
        ctx,
        trace=trace,
        sanitized_text=sanitized,
        batch=batch,
        max_steps=8,
    )
    trail.record_plan_out(invocation_id="A", plan=out_a)
    assert out_a is not None
    assert out_a.get("tool_name") == "k8s_patch_secret"
    assert not sim.secret_patched

    mem_a = await load_trace_memory(redis, trace, initial_symptoms=sanitized, seed_attempt=0)
    assert any(r.kind == "mutate_planned" and r.tool_name == "k8s_patch_secret" for r in mem_a.action_history)

    trail.record_executor_sim(
        invocation_id="A",
        tool_name="k8s_patch_secret",
        detail="apply_patch_secret(sim)",
    )
    sim.apply_patch_secret(dict(out_a.get("args") or {}))
    assert sim.secret_patched is True

    stub.reset("B")
    out_b = await aal.run_agentic_mutate_plan(
        ctx,
        trace=trace,
        sanitized_text=sanitized,
        batch=batch,
        max_steps=8,
    )
    trail.record_plan_out(invocation_id="B", plan=out_b)

    assert out_b is not None
    assert out_b.get("reason_code") == PLANNER_PHASE_DONE
    assert str(out_b.get("resolution_summary") or "").strip()
    mem_b = await load_trace_memory(redis, trace, initial_symptoms=sanitized, seed_attempt=0)
    kinds = [r.kind for r in mem_b.action_history]
    assert "readonly_executed" in kinds
    assert "mutate_planned" in kinds
    assert "phase_done" in kinds

    assert_audit_sequence_kinds(
        trail,
        [
            "llm_round",
            "readonly_executed",
            "llm_round",
            "plan_result",
            "executor_simulated",
            "llm_round",
            "llm_round",
            "plan_result",
        ],
    )
    trail.maybe_write_json()
    if os.environ.get("OMNI_E2E_AUDIT_JSON"):
        assert os.path.isfile(os.environ["OMNI_E2E_AUDIT_JSON"])
