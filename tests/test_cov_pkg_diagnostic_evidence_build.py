"""Coverage: diagnostic_policy, evidence_signals, build_reasoning_chain."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pkg.reasoning import diagnostic_policy, evidence_signals
from pkg.reasoning.reason_codes import (
    INV_DISCOVERY_MANDATORY,
    INV_NAMESPACE_ISOLATION,
    INV_NO_RESTART_ON_BROKEN_SPEC,
    INV_READ_BEFORE_MUTATE,
)


def test_evidence_suggests_broken_spec_table() -> None:
    cases = [
        ([{"alert_hint": "ConfigMap not found"}], True),
        ([{"extracted_fact": {"message": "FailedMount secret not found"}}], True),
        ([{"alert_hint": "healthy"}], False),
    ]
    for batch, exp in cases:
        assert diagnostic_policy.evidence_suggests_broken_spec(batch) is exp


def test_batch_has_prior_readonly_evidence() -> None:
    assert diagnostic_policy.batch_has_prior_readonly_evidence(
        [{"probe": "k8s_clinical_pod_status", "result": "ok"}]
    )
    assert not diagnostic_policy.batch_has_prior_readonly_evidence([{"probe": "unknown"}])


def test_discovery_mandatory_satisfied() -> None:
    ok, meta = diagnostic_policy.discovery_mandatory_satisfied(
        [{"probe": "prometheus_query"}],
        discovery_steps=["query_prometheus_metrics"],
        readonly_executed=None,
    )
    assert ok and "discovery_via_tool" in meta

    ok2, meta2 = diagnostic_policy.discovery_mandatory_satisfied(
        [],
        discovery_steps=[],
        readonly_executed=[],
    )
    assert not ok2 and meta2.get("discovery_missing")


def test_discovery_satisfying_tool_names_nonempty() -> None:
    names = diagnostic_policy.discovery_satisfying_tool_names()
    assert "query_prometheus_metrics" in names


@pytest.mark.parametrize(
    "tool,batch,disc,proof,expect_code",
    [
        (
            "k8s_rollout_restart",
            [{"raw": "FailedMount configmap not found"}],
            [],
            "state",
            INV_NO_RESTART_ON_BROKEN_SPEC,
        ),
        (
            "k8s_rollout_restart",
            [{"alert_hint": "CrashLoopBackOff on pod"}],
            [],
            "state",
            INV_READ_BEFORE_MUTATE,
        ),
        (
            "k8s_scale_deployment",
            [{"alert_hint": "OOMKilled workload"}],
            [],
            "state",
            INV_READ_BEFORE_MUTATE,
        ),
    ],
)
def test_evaluate_diagnostic_invariants_mutate_blocks(
    tool: str,
    batch: list,
    disc: list,
    proof: str,
    expect_code: str,
) -> None:
    ws = SimpleNamespace(omni_discovery_mandatory=False, autonomous_allowed_namespaces="multi-agent")
    ok, code, _meta = diagnostic_policy.evaluate_diagnostic_invariants(
        ws,
        tool_name=tool,
        args={"namespace": "multi-agent", "deployment": "d"},
        batch=batch,
        discovery_tool_names=disc,
        proof_lane=proof,
        readonly_discovery_executed=None,
    )
    assert ok is False and code == expect_code


def test_evaluate_diagnostic_namespace_isolation() -> None:
    ws = SimpleNamespace(omni_discovery_mandatory=False, autonomous_allowed_namespaces="multi-agent")
    ok, code, meta = diagnostic_policy.evaluate_diagnostic_invariants(
        ws,
        tool_name="k8s_rollout_restart",
        args={"namespace": "other-ns-not-allowed"},
        batch=[],
        discovery_tool_names=[],
        proof_lane="resource",
    )
    assert ok is False and code == INV_NAMESPACE_ISOLATION
    assert meta.get("security_signal") is True


def test_evaluate_diagnostic_discovery_mandatory() -> None:
    ws = SimpleNamespace(
        omni_discovery_mandatory=True,
        autonomous_allowed_namespaces="multi-agent",
    )
    ok, code, _meta = diagnostic_policy.evaluate_diagnostic_invariants(
        ws,
        tool_name="k8s_rollout_restart",
        args={"namespace": "multi-agent"},
        batch=[],
        discovery_tool_names=[],
        proof_lane="state",
        readonly_discovery_executed=[],
    )
    assert ok is False and code == INV_DISCOVERY_MANDATORY


def test_evaluate_rollout_ok_with_discovery() -> None:
    ws = SimpleNamespace(omni_discovery_mandatory=False, autonomous_allowed_namespaces="multi-agent")
    ok, code, _meta = diagnostic_policy.evaluate_diagnostic_invariants(
        ws,
        tool_name="k8s_rollout_restart",
        args={"namespace": "multi-agent"},
        batch=[{"alert_hint": "CrashLoopBackOff"}],
        discovery_tool_names=["k8s_describe_resource"],
        proof_lane="state",
    )
    assert ok is True and code is None


def test_build_reasoning_chain_payload() -> None:
    p = diagnostic_policy.build_reasoning_chain_payload(
        verdict="V",
        lane="L",
        thought_process=["a", "b"],
        invariant_id="INV_X",
    )
    assert p["verdict"] == "V" and p["invariant_id"] == "INV_X"


def test_critical_evidence_present_table() -> None:
    assert evidence_signals.critical_evidence_present([{"alert_hint": "ImagePullBackOff"}])
    snip = json.dumps({"labels": {"reason": "OOMKilled", "alertname": "x"}})
    assert evidence_signals.critical_evidence_present(
        [{"canonical_query_snippet": snip, "alert_hint": ""}]
    )
    assert not evidence_signals.critical_evidence_present([{"alert_hint": "latency ok"}])
