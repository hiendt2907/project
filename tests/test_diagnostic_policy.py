"""Deterministic diagnostic policy invariants (INV_*)."""

from __future__ import annotations

from types import SimpleNamespace

from pkg.reasoning.diagnostic_policy import (
    batch_has_prior_readonly_evidence,
    evidence_suggests_broken_spec,
    evaluate_diagnostic_invariants,
)
from pkg.reasoning.evidence_signals import critical_evidence_present
from pkg.reasoning.incident_matrix_profile import invalidate_matrix_cache, resolve_proof_lane
from pkg.reasoning.reason_codes import (
    INV_NAMESPACE_ISOLATION,
    INV_NO_RESTART_ON_BROKEN_SPEC,
    INV_READ_BEFORE_MUTATE,
    INV_READ_BEFORE_MUTATE_DEFER,
)


def test_inv_no_restart_meta_concern_zone_any_lane() -> None:
    batch = [
        {
            "probe": "k8s_clinical_pod_events",
            "alert_hint": "CreateContainerConfigError",
            "extracted_fact": {"message": 'configmap "nginx-cfg" not found'},
        }
    ]
    ws = SimpleNamespace(autonomous_allowed_namespaces="multi-agent", env_mode="prod")
    ok, reason, meta = evaluate_diagnostic_invariants(
        ws,
        tool_name="k8s_rollout_restart",
        args={"namespace": "multi-agent", "deployment": "nginx-test"},
        batch=batch,
        discovery_tool_names=[],
        proof_lane="resource",
    )
    assert ok is False
    assert reason == INV_NO_RESTART_ON_BROKEN_SPEC
    assert meta.get("inv_no_restart_concern_zone") is True
    assert meta.get("evidence_suggests_broken_spec") is True


def test_inv_no_restart_on_broken_spec_blocks_rollout() -> None:
    batch = [
        {
            "probe": "k8s_clinical_pod_events",
            "alert_hint": "CreateContainerConfigError",
            "extracted_fact": {"message": 'configmap "nginx-cfg" not found'},
        }
    ]
    assert evidence_suggests_broken_spec(batch) is True
    ws = SimpleNamespace(autonomous_allowed_namespaces="multi-agent", env_mode="prod")
    ok, reason, _ = evaluate_diagnostic_invariants(
        ws,
        tool_name="k8s_rollout_restart",
        args={"namespace": "multi-agent", "deployment": "nginx-test"},
        batch=batch,
        discovery_tool_names=["inspect_pod_deep"],
        proof_lane="resource",
    )
    assert ok is False
    assert reason == INV_NO_RESTART_ON_BROKEN_SPEC


def test_inv_read_before_mutate_alias_matches_spec() -> None:
    assert INV_READ_BEFORE_MUTATE_DEFER == INV_READ_BEFORE_MUTATE


def test_inv_read_before_mutate_defers_without_discovery() -> None:
    batch = [
        {
            "probe": "batch_diagnostic_evidence",
            "alert_hint": "CrashLoopBackOff",
            "canonical_query_snippet": '{"labels": {"reason": "CrashLoopBackOff", "alertname": "X"}}',
        }
    ]
    assert critical_evidence_present(batch) is True
    assert batch_has_prior_readonly_evidence(batch) is False
    ws = SimpleNamespace(autonomous_allowed_namespaces="multi-agent", env_mode="prod")
    ok, reason, _ = evaluate_diagnostic_invariants(
        ws,
        tool_name="k8s_rollout_restart",
        args={"namespace": "multi-agent", "deployment": "d"},
        batch=batch,
        discovery_tool_names=[],
        proof_lane="state",
    )
    assert ok is False
    assert reason == INV_READ_BEFORE_MUTATE


def test_inv_rollout_resource_lane_allows_without_react_discovery() -> None:
    """Read-before defer for rollout does not apply when proof_lane is resource."""
    batch = [
        {
            "probe": "batch_diagnostic_evidence",
            "alert_hint": "CrashLoopBackOff",
            "canonical_query_snippet": '{"labels": {"reason": "CrashLoopBackOff", "alertname": "X"}}',
        }
    ]
    assert critical_evidence_present(batch) is True
    assert batch_has_prior_readonly_evidence(batch) is False
    ws = SimpleNamespace(autonomous_allowed_namespaces="multi-agent", env_mode="prod")
    ok, reason, _ = evaluate_diagnostic_invariants(
        ws,
        tool_name="k8s_rollout_restart",
        args={"namespace": "multi-agent", "deployment": "d"},
        batch=batch,
        discovery_tool_names=[],
        proof_lane="resource",
    )
    assert ok is True
    assert reason is None


def test_inv_read_before_passes_with_prior_evidence() -> None:
    batch = [
        {
            "probe": "k8s_clinical_pod_status",
            "alert_hint": "CrashLoopBackOff",
            "extracted_fact": {"reason": "CrashLoopBackOff", "message": "back-off restarting"},
        }
    ]
    assert batch_has_prior_readonly_evidence(batch) is True
    ws = SimpleNamespace(autonomous_allowed_namespaces="multi-agent", env_mode="prod")
    ok, reason, _ = evaluate_diagnostic_invariants(
        ws,
        tool_name="k8s_rollout_restart",
        args={"namespace": "multi-agent", "deployment": "d"},
        batch=batch,
        discovery_tool_names=[],
        proof_lane="state",
    )
    assert ok is True
    assert reason is None


def test_inv_namespace_isolation() -> None:
    batch = [{"probe": "p", "alert_hint": "waiting"}]
    ws = SimpleNamespace(autonomous_allowed_namespaces="multi-agent", env_mode="prod")
    ok, reason, meta = evaluate_diagnostic_invariants(
        ws,
        tool_name="k8s_rollout_restart",
        args={"namespace": "kube-system", "deployment": "x"},
        batch=batch,
        discovery_tool_names=[],
    )
    assert ok is False
    assert reason == INV_NAMESPACE_ISOLATION
    assert meta.get("security_signal") is True


def test_resolve_proof_lane_blind_hint() -> None:
    invalidate_matrix_cache()
    batch = [{"probe": "x", "alert_hint": "noise"}]
    lane, src = resolve_proof_lane(batch, rag_match_text=None, blind_lane_hint="app_log")
    assert lane == "app_log"
    assert src == "blind_hint"
