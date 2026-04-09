"""Autonomous EXECUTE_MUTATE contract and feedback."""

from __future__ import annotations

import json
from types import SimpleNamespace
import pytest

from pkg.autonomous_actions import (
    ACTION_EXECUTE_MUTATE,
    ACTION_SUGGEST_REMEDIATION,
    build_action_feedback_body,
    build_execute_mutate_body,
    infer_exit_code_from_tool_output,
)
from messaging.kafka_bus import decode_kafka_value_to_fields
from workers.evidence_mutate_emit import (
    rollout_args_from_evidence_batch,
    should_emit_rollout_after_rag,
    workload_cpu_incident_rollout_eligible,
    workload_fault_incident_rollout_eligible,
)
from workers.kafka_actions_consumer import _omni_actions_body_preview


def test_build_execute_mutate_body() -> None:
    b = build_execute_mutate_body(
        "tr-1",
        tool_name="k8s_rollout_restart",
        args={"namespace": "ns", "deployment": "dep"},
        attempt_count=2,
    )
    assert b["action"] == ACTION_EXECUTE_MUTATE
    assert b["trace_id"] == "tr-1"
    assert b["data"]["tool_name"] == "k8s_rollout_restart"
    assert b["data"]["args"]["namespace"] == "ns"
    assert b["data"]["attempt_count"] == 2
    assert b["data"]["correlation_id"]


def test_build_execute_mutate_body_reasoning_chain_optional() -> None:
    rc = {"verdict": "EXECUTE_PLAN", "lane": "state", "thought_process": ["OBSERVATION: x"]}
    b = build_execute_mutate_body(
        "tr-rc",
        tool_name="k8s_rollout_restart",
        args={"namespace": "ns", "deployment": "dep"},
        attempt_count=1,
        reasoning_chain=rc,
    )
    assert b["data"]["reasoning_chain"] == rc
    assert "proof_of_fault" not in b["data"]


def test_build_action_feedback_body() -> None:
    fb = build_action_feedback_body(
        trace_id="t1",
        tool_name="k8s_rollout_restart",
        correlation_id="c1",
        stdout="ok",
        stderr="",
        exit_code=0,
        mutate_args={"namespace": "ns", "deployment": "dep"},
    )
    assert fb["exit_code"] == 0
    assert fb["stdout"] == "ok"
    assert fb["mutate_args"]["namespace"] == "ns"


def test_infer_exit_code() -> None:
    assert infer_exit_code_from_tool_output("[DATA] error\n[DIAGNOSIS] x") == 1
    assert infer_exit_code_from_tool_output("[DATA] rollout_restart_ok") == 0


def test_executor_preview_execute_mutate() -> None:
    inner = build_execute_mutate_body(
        "tr-9",
        tool_name="k8s_rollout_restart",
        args={"namespace": "a", "deployment": "b"},
        attempt_count=1,
    )
    p = _omni_actions_body_preview(inner)
    assert "k8s_rollout_restart" in p
    assert "attempt_count" in p.lower() or "1" in p


def test_rollout_args_from_batch() -> None:
    batch = [
        {
            "canonical_query_snippet": '{"labels": {"namespace": "prod", "deployment": "api", "pod": "x"}}',
        }
    ]
    r = rollout_args_from_evidence_batch(batch)
    assert r == {"namespace": "prod", "deployment": "api"}


def test_workload_cpu_incident_rollout_eligible() -> None:
    batch_cpu = [
        {
            "alert_hint": "HighCPUUsage Container nginx CPU utilization ~90%",
            "canonical_query_snippet": '{"labels": {"alertname": "HighCPUUsage", "namespace": "ns", "deployment": "d"}}',
        }
    ]
    assert workload_cpu_incident_rollout_eligible(batch_cpu) is True
    batch_plain = [{"alert_hint": "Pod not scheduled"}]
    assert workload_cpu_incident_rollout_eligible(batch_plain) is False


def test_workload_fault_incident_rollout_eligible() -> None:
    batch_fault = [
        {
            "alert_hint": "CreateContainerError for nginx-test",
            "canonical_query_snippet": '{"labels": {"namespace": "ns", "deployment": "dep", "reason": "CreateContainerError"}}',
        }
    ]
    assert workload_fault_incident_rollout_eligible(batch_fault) is True
    batch_plain = [{"alert_hint": "CPU below baseline"}]
    assert workload_fault_incident_rollout_eligible(batch_plain) is False


def test_should_emit_rollout_after_rag_cpu_incident() -> None:
    batch = [
        {
            "alert_hint": "HighCPUUsage ...",
            "canonical_query_snippet": '{"labels": {"namespace": "ns", "deployment": "dep", "alertname": "HighCPUUsage"}}',
        }
    ]
    rr = rollout_args_from_evidence_batch(batch)
    assert rr == {"namespace": "ns", "deployment": "dep"}
    assert (
        should_emit_rollout_after_rag(
            suggested_tool="kubectl_get_events",
            # Avoid substring "restart"/"rollout" — should_try_rollout_from_rag matches naïvely.
            diag_snippet="inspect events and pod conditions",
            batch=batch,
            rr=rr,
            autonomous_rollout_on_cpu_incident=True,
            autonomous_rollout_on_fault_incident=False,
        )
        is True
    )
    assert (
        should_emit_rollout_after_rag(
            suggested_tool="kubectl_get_events",
            diag_snippet="inspect events and pod conditions",
            batch=batch,
            rr=rr,
            autonomous_rollout_on_cpu_incident=False,
            autonomous_rollout_on_fault_incident=False,
        )
        is False
    )


def test_should_emit_rollout_after_rag_fault_incident() -> None:
    batch = [
        {
            "alert_hint": "Readiness probe failing and CrashLoopBackOff",
            "canonical_query_snippet": '{"labels": {"namespace": "ns", "deployment": "dep", "alertname": "ProbeFailureLab"}}',
        }
    ]
    rr = rollout_args_from_evidence_batch(batch)
    assert rr == {"namespace": "ns", "deployment": "dep"}
    assert (
        should_emit_rollout_after_rag(
            suggested_tool="kubectl_get_events",
            diag_snippet="inspect events and pod conditions",
            batch=batch,
            rr=rr,
            autonomous_rollout_on_cpu_incident=False,
            autonomous_rollout_on_fault_incident=True,
        )
        is True
    )


def test_suggest_action_constant() -> None:
    from workers.omni_actions_remediation import ACTION_SUGGEST_REMEDIATION as SUG

    assert SUG == ACTION_SUGGEST_REMEDIATION


def test_mutate_allowlist_k8s_sdk_only() -> None:
    from workers.autonomous_execute import (
        K8S_SDK_MUTATING_TOOL_NAMES,
        MUTATE_TOOL_ALLOWLIST,
        MUTATE_TOOL_REGISTRY_NAME,
        READONLY_TOOL_ALLOWLIST,
    )
    from workers.tools import TOOL_REGISTRY

    missing = sorted(K8S_SDK_MUTATING_TOOL_NAMES - TOOL_REGISTRY.keys())
    assert not missing, f"K8S_SDK_MUTATING_TOOL_NAMES has names not in TOOL_REGISTRY: {missing}"
    assert "echo" not in K8S_SDK_MUTATING_TOOL_NAMES
    assert "execute_shell_command" not in K8S_SDK_MUTATING_TOOL_NAMES
    assert "k8s_describe_resource" in READONLY_TOOL_ALLOWLIST
    assert "k8s_describe_resource" not in MUTATE_TOOL_ALLOWLIST
    assert MUTATE_TOOL_REGISTRY_NAME.get("k8s_patch_deployment") == "k8s_patch_resource"
    assert "k8s_patch_deployment" in MUTATE_TOOL_ALLOWLIST
    assert len(MUTATE_TOOL_ALLOWLIST) == len(K8S_SDK_MUTATING_TOOL_NAMES) + len(MUTATE_TOOL_REGISTRY_NAME)


@pytest.mark.asyncio
async def test_execute_mutate_rejects_non_k8s_tool() -> None:
    from workers.autonomous_execute import run_execute_mutate_tool

    out, code = await run_execute_mutate_tool(
        None,
        tool_name="echo",
        args={"msg": "x"},
        trace_id="t-lab",
    )
    assert code == 1
    assert "not in mutate-only allowlist" in out


@pytest.mark.asyncio
async def test_execute_mutate_rejects_readonly_tool() -> None:
    from workers.autonomous_execute import run_execute_mutate_tool

    out, code = await run_execute_mutate_tool(
        None,
        tool_name="k8s_describe_resource",
        args={"resource_type": "Pod", "name": "x", "namespace": "multi-agent"},
        trace_id="t-lab",
    )
    assert code == 1
    assert "read_only_tool_blocked" in out


def test_decode_kafka_fields_prefers_trace_header() -> None:
    body = {"trace_id": "payload-trace", "data": '{"trace_id":"inner-trace"}'}
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    fields = decode_kafka_value_to_fields(raw, [("trace_id", b"header-trace")])
    assert fields["trace_id"] == "header-trace"


def test_normalize_describe_resource_args_from_kind() -> None:
    from workers.autonomous_execute import _normalize_mutate_args_for_registry

    args = _normalize_mutate_args_for_registry(
        "k8s_describe_resource",
        {"kind": "pod", "pod": "nginx-test-abc", "namespace": "multi-agent"},
    )
    assert args["resource_type"] == "Pod"
    assert args["name"] == "nginx-test-abc"


def test_settings_prod_mode_disables_lab_bypass_flags() -> None:
    from workers.settings import WorkerSettings

    ws = WorkerSettings(
        env_mode="prod",
        god_mode=True,
        lab_unchained=True,
        cluster_full_access=True,
        proactive_fallback_bypass_policy_in_god_mode=True,
    )
    assert ws.env_mode == "prod"
    assert ws.god_mode is False
    assert ws.lab_unchained is False
    assert ws.cluster_full_access is False
    assert ws.proactive_fallback_bypass_policy_in_god_mode is False


@pytest.mark.asyncio
async def test_execute_mutate_prod_namespace_policy_denied() -> None:
    from workers.autonomous_execute import run_execute_mutate_tool

    ctx = SimpleNamespace(
        settings=SimpleNamespace(env_mode="prod", autonomous_allowed_namespaces="multi-agent"),
    )
    out, code = await run_execute_mutate_tool(
        ctx,
        tool_name="k8s_scale_deployment",
        args={"namespace": "other-ns", "deployment": "api", "replicas": 1},
        trace_id="t-prod",
    )
    assert code == 1
    assert "prod_mode_policy_denied" in out


@pytest.mark.asyncio
async def test_execute_mutate_dev_mode_allows_nonlisted_namespace() -> None:
    from workers.autonomous_execute import run_execute_mutate_tool
    from workers.tools import TOOL_REGISTRY

    async def _stub(_ctx: object, _args: dict) -> str:
        return "[DATA] ok"

    prev = TOOL_REGISTRY.get("k8s_scale_deployment")
    TOOL_REGISTRY["k8s_scale_deployment"] = _stub
    try:
        ctx = SimpleNamespace(
            settings=SimpleNamespace(env_mode="dev", autonomous_allowed_namespaces="multi-agent"),
        )
        out, code = await run_execute_mutate_tool(
            ctx,
            tool_name="k8s_scale_deployment",
            args={"namespace": "other-ns", "deployment": "api", "replicas": 1},
            trace_id="t-dev",
        )
        assert code == 0
        assert "[DATA] ok" in out
    finally:
        if prev is None:
            TOOL_REGISTRY.pop("k8s_scale_deployment", None)
        else:
            TOOL_REGISTRY["k8s_scale_deployment"] = prev
