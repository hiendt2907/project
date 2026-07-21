"""Phase 0b: canonical cross-lane contracts (src/pkg/contracts/).

Proves the K8s-lane and VM-lane Evidence shapes both convert losslessly
through CanonicalEvidence — the actual Exit Criteria for this phase, not
just "the module imports."
"""
from __future__ import annotations

from pkg.contracts.evidence import (
    from_agent_evidence_item,
    from_diagnostic_evidence_dict,
    to_agent_evidence_item,
    to_diagnostic_evidence_dict,
)
from pkg.contracts.identity import CorrelationIdentity


def test_k8s_evidence_round_trips_losslessly():
    original = {
        "kind": "diagnostic", "trace_id": "gw-abc123", "symptom_group": "cpu",
        "layer": "os", "lane": "SYS_RESOURCE", "probe": "cpu_zscore",
        "result": "FAILED", "extracted_fact": {"z": 3.2},
        "raw": "z-score 3.2 sustained 5m", "ts": "2026-07-21T00:00:00Z",
        "namespace": "multi-agent", "alert_rule": "OmniCpuHigh",
        "alert_hint": "CPU sustained high", "canonical_query_snippet": "cpu_usage",
        "evidence_source": "prober", "clinical_priority_note": "urgent",
        "tenant_id": "staging-sim", "agent_id": "agent-1", "hostname": "cust-app",
    }
    canonical = from_diagnostic_evidence_dict(original)
    round_tripped = to_diagnostic_evidence_dict(canonical)
    for key, value in original.items():
        assert round_tripped[key] == value, f"{key}: {round_tripped[key]!r} != {value!r}"


def test_vm_evidence_round_trips_losslessly():
    original = {
        "trace_id": "trace-1", "probe": "remote_system_metrics", "result": "FAILED",
        "extracted_fact": {"cpu_pct": 97.1}, "raw": "cpu 97.1%",
        "symptom_group": "cpu", "lane": "SYS_RESOURCE", "lane_hint": "SYS_RESOURCE",
        "lane_authoritative": True, "stream_tags": ["cpu", "hot"],
        "namespace": "cust-app", "ts": "2026-07-21T00:00:00Z",
        "alert_rule": "RemoteAgentAlert", "alert_hint": "cpu high",
        "evidence_source": "RemoteAgent", "signal_type": "ANOMALY",
    }
    canonical = from_agent_evidence_item(
        original, agent_id="staging-sim_cust-app", hostname="cust-app", tenant_id="staging-sim",
    )
    assert canonical.agent_id == "staging-sim_cust-app"
    assert canonical.tenant_id == "staging-sim"
    round_tripped = to_agent_evidence_item(canonical)
    for key, value in original.items():
        if key == "stream_tags":
            assert list(round_tripped[key]) == value
        else:
            assert round_tripped[key] == value, f"{key}: {round_tripped[key]!r} != {value!r}"


def test_k8s_and_vm_evidence_for_the_same_incident_produce_equal_core_fields():
    """The whole point of a canonical shape: two lanes describing the same
    thing should be comparable on their shared fields, not just structurally
    similar dicts with different key sets."""
    k8s = from_diagnostic_evidence_dict({
        "trace_id": "t-1", "probe": "p", "result": "FAILED",
        "lane": "SYS_RESOURCE", "namespace": "ns-1",
    })
    vm = from_agent_evidence_item(
        {"trace_id": "t-1", "probe": "p", "result": "FAILED",
         "lane": "SYS_RESOURCE", "namespace": "ns-1"},
        agent_id="a-1", hostname="h-1", tenant_id="ten-1",
    )
    assert k8s.trace_id == vm.trace_id
    assert k8s.probe == vm.probe
    assert k8s.result == vm.result
    assert k8s.lane == vm.lane
    assert k8s.namespace == vm.namespace


def test_extracted_fact_string_json_is_parsed_not_left_as_string():
    """DiagnosticEvidenceDict declares extracted_fact: str (it can arrive
    pre-serialized from Kafka) — canonical form is always a dict."""
    canonical = from_diagnostic_evidence_dict({
        "trace_id": "t", "probe": "p", "result": "FAILED",
        "extracted_fact": '{"cpu_pct": 90}',
    })
    assert canonical.extracted_fact == {"cpu_pct": 90}


def test_correlation_identity_is_fully_bound_requires_every_field():
    assert not CorrelationIdentity(tenant_id="acme").is_fully_bound()
    full = CorrelationIdentity(
        tenant_id="acme", mission_id="m1", incident_id="i1",
        decision_id="d1", action_id="a1", command_id="c1",
    )
    assert full.is_fully_bound()


def test_correlation_identity_from_dict_extracts_known_keys():
    identity = CorrelationIdentity.from_dict({
        "tenant": "acme", "mission_id": "m1", "incident_id": "i1",
        "decision_id": "d1", "action_id": "a1", "command_id": "c1", "unit": "ignored",
    })
    assert identity == CorrelationIdentity(
        tenant_id="acme", mission_id="m1", incident_id="i1",
        decision_id="d1", action_id="a1", command_id="c1",
    )
