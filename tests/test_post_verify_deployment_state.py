"""Post-verify deployment gate: workload labels, sanitization."""

from __future__ import annotations

import json

from pkg.reasoning.alert_identity import parse_signal_dna_from_labels
from workers.post_verify_deployment_state import (
    resolve_namespace_deployment_for_state_gate,
    sanitize_probe_text_for_llm,
)
from workers.proactive_models import AnomalyEvent


def test_resolve_prefers_rollout_mutate_args():
    ev = AnomalyEvent(
        trace_id="trace-one",
        canonical_query=json.dumps({"labels": {"namespace": "ns1", "workload": "chaos-victim"}}),
        namespace="ns1",
        deployment="",
    )
    ns, dep = resolve_namespace_deployment_for_state_gate(
        {"namespace": "multi-agent", "deployment": "chaos-victim"},
        "k8s_rollout_restart",
        ev,
    )
    assert ns == "multi-agent"
    assert dep == "chaos-victim"


def test_resolve_from_canonical_workload_when_ev_deployment_empty():
    ev = AnomalyEvent(
        trace_id="trace-one",
        canonical_query=json.dumps(
            {
                "labels": {
                    "namespace": "multi-agent",
                    "workload": "chaos-victim",
                    "alertname": "KubePodCrashLoopVictim",
                }
            }
        ),
        namespace="multi-agent",
        deployment="",
    )
    ns, dep = resolve_namespace_deployment_for_state_gate({}, "", ev)
    assert ns == "multi-agent"
    assert dep == "chaos-victim"


def test_signal_dna_maps_workload_to_deployment():
    dna = parse_signal_dna_from_labels(
        {
            "alertname": "KubePodCrashLoopVictim",
            "namespace": "multi-agent",
            "workload": "chaos-victim",
        }
    )
    assert dna.deployment == "chaos-victim"


def test_sanitize_probe_text_replaces_pod_like_tokens():
    t = "pod chaos-victim-abc123def-xyz12 failed"
    out = sanitize_probe_text_for_llm(t, "chaos-victim")
    assert "chaos-victim-abc123def-xyz12" not in out
    assert "[chaos-victim:pod]" in out
