from __future__ import annotations

from types import SimpleNamespace

import pytest

from pkg.reasoning.reason_codes import (
    ERR_REA_HALLUCINATION_DETECTED,
    ERR_REA_SCHEMA_VIOLATION,
    ERR_SEM_CHANNEL_MISMATCH,
)
from workers.analyst_agentic_loop import (
    _planner_model_candidates,
    _readonly_tool_router,
    _reject_reason,
    coerce_k8s_readonly_args,
    run_agentic_mutate_plan,
)


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
    assert plan is not None
    assert plan["tool_name"] == "k8s_rollout_restart"
    assert plan["args"] == {"namespace": "multi-agent", "deployment": "nginx-test"}
    assert plan.get("discovery_steps") == []
    assert plan.get("reasoning_chain", {}).get("verdict") == "EXECUTE_PLAN"
    assert ollama.calls[:2] == ["bad:model", "deepseek-r1:8b"]


def test_coerce_k8s_describe_resource_from_kind_pod() -> None:
    a = coerce_k8s_readonly_args(
        "k8s_describe_resource",
        {"kind": "pod", "name": "p1", "namespace": "ns1"},
    )
    assert a["resource_type"] == "Pod"
    assert a["name"] == "p1"
    assert "kind" not in a


def test_coerce_k8s_describe_resource_normalizes_resource_type_pods() -> None:
    a = coerce_k8s_readonly_args(
        "k8s_describe_resource",
        {"resource_type": "pods", "name": "p1", "namespace": "ns1"},
    )
    assert a["resource_type"] == "Pod"


def test_coerce_k8s_describe_resource_configmap_unsupported_no_crash() -> None:
    raw = {"kind": "ConfigMap", "name": "cm", "namespace": "ns"}
    a = coerce_k8s_readonly_args("k8s_describe_resource", raw)
    assert a == raw


def test_coerce_k8s_other_tool_passthrough() -> None:
    a = coerce_k8s_readonly_args("inspect_pod_details", {"pod_name": "x", "namespace": "n"})
    assert a["pod_name"] == "x"


def test_readonly_tool_router_mutate_step_still_discovery() -> None:
    """Read-only allowlist always routes to discovery (semantic channel fix)."""
    assert _readonly_tool_router("k8s_describe_resource") is True
    assert _readonly_tool_router("inspect_pod_details") is True
    assert _readonly_tool_router("k8s_rollout_restart") is False


def test_reject_reason_taxonomy() -> None:
    assert _reject_reason(None) == ERR_REA_SCHEMA_VIOLATION
    assert _reject_reason({"tool_name": "", "args": {}}) == ERR_REA_SCHEMA_VIOLATION
    assert _reject_reason({"tool_name": "k8s_describe_resource", "args": {}}) == ERR_SEM_CHANNEL_MISMATCH
    assert _reject_reason({"tool_name": "not_allowed", "args": {}}) == ERR_REA_HALLUCINATION_DETECTED
    assert _reject_reason({"tool_name": "k8s_rollout_restart", "args": []}) == ERR_REA_SCHEMA_VIOLATION
    assert _reject_reason({"tool_name": "k8s_rollout_restart", "args": {"namespace": "ns"}}) == ERR_REA_SCHEMA_VIOLATION
