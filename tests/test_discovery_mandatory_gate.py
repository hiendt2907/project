"""Unit tests for OMNI_DISCOVERY_MANDATORY / diagnostic_policy discovery helpers."""

from __future__ import annotations

from pkg.reasoning.diagnostic_policy import (
    batch_has_prior_readonly_evidence,
    discovery_mandatory_satisfied,
    evaluate_diagnostic_invariants,
)
from pkg.reasoning.reason_codes import INV_DISCOVERY_MANDATORY
from workers.settings import WorkerSettings


def test_discovery_mandatory_unsatisfied_empty():
    ok, meta = discovery_mandatory_satisfied([], discovery_steps=[], readonly_executed=[])
    assert ok is False
    assert meta.get("discovery_missing") is True


def test_discovery_mandatory_satisfied_by_planner_step():
    ok, meta = discovery_mandatory_satisfied(
        [{"probe": "", "raw": ""}],
        discovery_steps=["k8s_describe_resource"],
        readonly_executed=[],
    )
    assert ok is True
    assert meta.get("discovery_via_tool") == "k8s_describe_resource"


def test_discovery_mandatory_satisfied_by_observability_probe():
    batch = [{"probe": "query_prometheus_metrics:intent=cpu", "raw": ""}]
    ok, meta = discovery_mandatory_satisfied(batch, discovery_steps=[], readonly_executed=[])
    assert ok is True
    assert meta.get("discovery_via_batch") is True


def test_batch_has_prior_promql_marker():
    assert batch_has_prior_readonly_evidence([{"raw": "instant promql query: up"}]) is True


def test_evaluate_diagnostic_invariant_discovery_mandatory_blocks_mutate():
    ws = WorkerSettings(omni_discovery_mandatory=True)
    batch = [{"probe": "", "raw": "", "alert_hint": ""}]
    ok, reason, _meta = evaluate_diagnostic_invariants(
        ws,
        tool_name="kubectl_cluster",
        args={},
        batch=batch,
        discovery_tool_names=[],
        proof_lane=None,
        readonly_discovery_executed=None,
    )
    assert ok is False
    assert reason == INV_DISCOVERY_MANDATORY


def test_evaluate_diagnostic_invariant_discovery_mandatory_allows_with_steps():
    ws = WorkerSettings(omni_discovery_mandatory=True)
    batch = [{"probe": "", "raw": "", "alert_hint": ""}]
    ok, reason, _meta = evaluate_diagnostic_invariants(
        ws,
        tool_name="kubectl_cluster",
        args={},
        batch=batch,
        discovery_tool_names=["query_prometheus_metrics"],
        proof_lane=None,
        readonly_discovery_executed=None,
    )
    assert ok is True
    assert reason is None


def test_default_settings_discovery_mandatory_off():
    ws = WorkerSettings()
    assert ws.omni_discovery_mandatory is False
