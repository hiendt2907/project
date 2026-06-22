"""Regression tests for auto-execute hardening (allowlist, governance, orchestrator phase)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pkg.trace_orchestrator.state import (
    TraceOrchestratorPhase,
    TraceOrchestratorState,
    load_trace_orchestrator_state,
    mark_trace_orchestrator_resolved_verified,
    redis_key_trace_orchestrator,
)
from workers.autonomous_execute import run_execute_mutate_tool
from workers.settings import WorkerSettings


@pytest.fixture
def prod_strict_settings() -> WorkerSettings:
    return WorkerSettings(
        env_mode="prod",
        omni_unrestricted_tool_execution=False,
        autonomous_allowed_namespaces="multi-agent",
        omni_auto_execute_enabled=False,
    )


async def test_restricted_blocks_readonly_mutate_channel(prod_strict_settings: WorkerSettings) -> None:
    ctx = SimpleNamespace(settings=prod_strict_settings)
    out, code = await run_execute_mutate_tool(
        ctx,
        tool_name="k8s_describe_resource",
        args={"namespace": "multi-agent", "name": "x", "resource_type": "Pod"},
        trace_id="t-readonly",
    )
    assert code != 0
    assert "ERR_GOV_UNAUTHORIZED_MUTATION" in out


async def test_restricted_blocks_unknown_tool(prod_strict_settings: WorkerSettings) -> None:
    ctx = SimpleNamespace(settings=prod_strict_settings)
    out, code = await run_execute_mutate_tool(
        ctx,
        tool_name="not_a_real_tool",
        args={},
        trace_id="t-unknown",
    )
    assert code != 0
    assert "ERR_GOV_UNAUTHORIZED_MUTATION" in out or "Unknown tool" in out


async def test_prod_namespace_out_of_bounds(prod_strict_settings: WorkerSettings) -> None:
    ctx = SimpleNamespace(settings=prod_strict_settings)
    out, code = await run_execute_mutate_tool(
        ctx,
        tool_name="k8s_scale_deployment",
        args={"namespace": "kube-system", "deployment": "coredns", "replicas": 1},
        trace_id="t-ns",
    )
    assert code != 0
    assert "ERR_GOV_NS_OUT_OF_BOUNDS" in out or "not in autonomous_allowed_namespaces" in out


async def test_prod_kubectl_cluster_blocked_even_when_unrestricted() -> None:
    ws = WorkerSettings(
        env_mode="prod",
        omni_unrestricted_tool_execution=True,
        omni_kubectl_cluster_mutate_allowed=False,
        autonomous_allowed_namespaces="multi-agent",
    )
    ctx = SimpleNamespace(settings=ws)
    out, code = await run_execute_mutate_tool(
        ctx,
        tool_name="kubectl_cluster",
        args={"argv": ["get", "pods"]},
        trace_id="t-kubectl",
    )
    assert code != 0
    assert "kubectl_cluster blocked" in out


async def test_prod_high_risk_tool_blocked_without_flag() -> None:
    ws = WorkerSettings(
        env_mode="prod",
        omni_unrestricted_tool_execution=False,
        omni_high_risk_mutate_allowed=False,
        autonomous_allowed_namespaces="multi-agent",
    )
    ctx = SimpleNamespace(settings=ws)
    out, code = await run_execute_mutate_tool(
        ctx,
        tool_name="k8s_delete_pod",
        args={"namespace": "multi-agent", "name": "dummy"},
        trace_id="t-high",
    )
    assert code != 0
    assert "high_risk_tool_blocked" in out


async def test_mark_trace_orchestrator_resolved_verified() -> None:
    store: dict[str, str] = {}

    class _FakeRedis:
        async def get(self, key: str):
            return store.get(key)

        async def setex(self, key: str, ttl: int, val: str) -> bool:
            store[key] = val
            return True

    r = _FakeRedis()
    tid = "trace-orc-1"
    st = TraceOrchestratorState(trace_id=tid, phase=TraceOrchestratorPhase.VERIFY)
    key = redis_key_trace_orchestrator(tid)
    await r.setex(key, 3600, json.dumps(st.to_dict()))

    assert await mark_trace_orchestrator_resolved_verified(r, tid) is True
    loaded = await load_trace_orchestrator_state(r, tid)
    assert loaded is not None
    assert loaded.phase == TraceOrchestratorPhase.RESOLVED
    assert loaded.last_verify_ok is True


async def test_mark_trace_orchestrator_noop_when_missing() -> None:
    class _EmptyRedis:
        async def get(self, key: str):
            return None

        async def setex(self, key: str, ttl: int, val: str) -> bool:
            raise AssertionError("should not save when state missing")

    assert await mark_trace_orchestrator_resolved_verified(_EmptyRedis(), "missing-trace") is True
