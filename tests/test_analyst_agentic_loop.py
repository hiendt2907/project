from __future__ import annotations

from types import SimpleNamespace

import pytest

from pkg.reasoning.reason_codes import (
    ERR_REA_HALLUCINATION_DETECTED,
    ERR_REA_SCHEMA_VIOLATION,
    ERR_SEM_CHANNEL_MISMATCH,
)
from workers.analyst_agentic_loop import _planner_model_candidates, _reject_reason, run_agentic_mutate_plan


def test_planner_model_candidates_order_and_dedup() -> None:
    ws = SimpleNamespace(
        diag_evidence_llm_model="",
        model_reasoning_engine="deepseek-r1:8b",
        model_helper="qwen2.5:1.5b",
        chat_model="qwen2.5:7b",
    )
    assert _planner_model_candidates(ws) == ["deepseek-r1:8b", "qwen2.5:1.5b", "qwen2.5:7b"]


@pytest.mark.asyncio
async def test_agentic_plan_fallback_model_when_first_model_404() -> None:
    class _OllamaStub:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def chat(self, *, model: str, messages: list[dict], stream: bool = False):  # noqa: ARG002
            self.calls.append(model)
            if model == "bad:model":
                raise RuntimeError("Client error '404 Not Found' for url 'http://ollama-service:11434/api/chat'")
            return {
                "message": {
                    "content": '{"tool_name":"k8s_rollout_restart","args":{"namespace":"multi-agent","deployment":"nginx-test"}}'
                }
            }

    ws = SimpleNamespace(
        diag_evidence_llm_model="bad:model",
        model_reasoning_engine="deepseek-r1:8b",
        model_helper="qwen2.5:1.5b",
        chat_model="qwen2.5:7b",
    )
    ollama = _OllamaStub()
    ctx = SimpleNamespace(settings=ws, ollama=ollama)
    plan = await run_agentic_mutate_plan(
        ctx,
        trace="t-1",
        sanitized_text="pod waiting CreateContainerConfigError",
        batch=[{"probe": "k8s_clinical_pod_status"}],
        max_steps=1,
    )
    assert plan == {
        "tool_name": "k8s_rollout_restart",
        "args": {"namespace": "multi-agent", "deployment": "nginx-test"},
    }
    assert ollama.calls[:2] == ["bad:model", "deepseek-r1:8b"]


def test_reject_reason_taxonomy() -> None:
    assert _reject_reason(None) == ERR_REA_SCHEMA_VIOLATION
    assert _reject_reason({"tool_name": "", "args": {}}) == ERR_REA_SCHEMA_VIOLATION
    assert _reject_reason({"tool_name": "k8s_describe_resource", "args": {}}) == ERR_SEM_CHANNEL_MISMATCH
    assert _reject_reason({"tool_name": "not_allowed", "args": {}}) == ERR_REA_HALLUCINATION_DETECTED
    assert _reject_reason({"tool_name": "k8s_rollout_restart", "args": []}) == ERR_REA_SCHEMA_VIOLATION
    assert _reject_reason({"tool_name": "k8s_rollout_restart", "args": {"namespace": "ns"}}) == ERR_REA_SCHEMA_VIOLATION
