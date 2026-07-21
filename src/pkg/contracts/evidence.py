"""Canonical Evidence contract — unifies the 3 divergent shapes found in the
Phase 0 audit:

  - K8s lane:  DiagnosticEvidenceDict (src/pkg/reasoning/schema.py)
  - VM lane:   EvidenceItem / AgentEvidenceRequest (src/gateway/routes/agent_webhook.py)
  - legacy:    EvidenceObject (src/workers/diagnostic_evidence.py, probe_name/raw_output
               naming — 1 known importer; not adapted here, flagged in Phase 6 audit)

The two live shapes are semantically close (same core fields under the same
names: trace_id/probe/result/extracted_fact/raw/ts/namespace/alert_rule/
alert_hint/evidence_source/lane) — EvidenceItem is a superset (adds
lane_hint/lane_authoritative/stream_tags/signal_type). CanonicalEvidence is
that superset; fields absent from one side default to their DiagnosticEvidenceDict-equivalent
neutral value on that side's own adapter.

This module is additive only — it does not change what evidence_consumer.py
or agent_webhook.py do with evidence today. Wiring it into those call sites
is explicitly out of scope for Phase 0b (see src/pkg/contracts/__init__.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CanonicalEvidence:
    trace_id: str
    probe: str
    result: str
    extracted_fact: dict[str, Any] = field(default_factory=dict)
    raw: str = ""
    symptom_group: str = ""
    lane: str = ""
    namespace: str = ""
    ts: str = ""
    alert_rule: str = ""
    alert_hint: str = ""
    evidence_source: str = ""
    tenant_id: str = ""
    agent_id: str = ""
    hostname: str = ""
    # VM-lane-specific (default neutral on the K8s side)
    lane_hint: str = ""
    lane_authoritative: bool = False
    stream_tags: tuple[str, ...] = ()
    signal_type: str = "ANOMALY"
    # K8s-lane-specific (default neutral on the VM side)
    kind: str = ""
    layer: str = ""
    canonical_query_snippet: str = ""
    clinical_priority_note: str = ""


def from_diagnostic_evidence_dict(d: dict) -> CanonicalEvidence:
    """K8s lane (src/pkg/reasoning/schema.py::DiagnosticEvidenceDict) -> canonical."""
    extracted = d.get("extracted_fact")
    if isinstance(extracted, str):
        import json
        try:
            extracted = json.loads(extracted) if extracted else {}
        except Exception:
            extracted = {"_raw": extracted}
    return CanonicalEvidence(
        trace_id=str(d.get("trace_id", "")),
        probe=str(d.get("probe", "")),
        result=str(d.get("result", "")),
        extracted_fact=extracted if isinstance(extracted, dict) else {},
        raw=str(d.get("raw", "")),
        symptom_group=str(d.get("symptom_group", "")),
        lane=str(d.get("lane", "")),
        namespace=str(d.get("namespace", "")),
        ts=str(d.get("ts", "")),
        alert_rule=str(d.get("alert_rule", "")),
        alert_hint=str(d.get("alert_hint", "")),
        evidence_source=str(d.get("evidence_source", "")),
        tenant_id=str(d.get("tenant_id", "")),
        agent_id=str(d.get("agent_id", "")),
        hostname=str(d.get("hostname", "")),
        kind=str(d.get("kind", "")),
        layer=str(d.get("layer", "")),
        canonical_query_snippet=str(d.get("canonical_query_snippet", "")),
        clinical_priority_note=str(d.get("clinical_priority_note", "")),
    )


def to_diagnostic_evidence_dict(e: CanonicalEvidence) -> dict:
    """canonical -> K8s lane shape. Round-trips from_diagnostic_evidence_dict()
    losslessly for every field DiagnosticEvidenceDict declares."""
    return {
        "trace_id": e.trace_id, "probe": e.probe, "result": e.result,
        "extracted_fact": e.extracted_fact, "raw": e.raw,
        "symptom_group": e.symptom_group, "lane": e.lane, "namespace": e.namespace,
        "ts": e.ts, "alert_rule": e.alert_rule, "alert_hint": e.alert_hint,
        "evidence_source": e.evidence_source, "tenant_id": e.tenant_id,
        "agent_id": e.agent_id, "hostname": e.hostname, "kind": e.kind,
        "layer": e.layer, "canonical_query_snippet": e.canonical_query_snippet,
        "clinical_priority_note": e.clinical_priority_note,
    }


def from_agent_evidence_item(item: dict, *, agent_id: str = "", hostname: str = "",
                             tenant_id: str = "") -> CanonicalEvidence:
    """VM lane (src/gateway/routes/agent_webhook.py::EvidenceItem, as a plain
    dict via .model_dump()) -> canonical. agent_id/hostname/tenant_id come
    from the wrapping AgentEvidenceRequest, not EvidenceItem itself."""
    return CanonicalEvidence(
        trace_id=str(item.get("trace_id", "")),
        probe=str(item.get("probe", "")),
        result=str(item.get("result", "")),
        extracted_fact=dict(item.get("extracted_fact") or {}),
        raw=str(item.get("raw", "")),
        symptom_group=str(item.get("symptom_group", "")),
        lane=str(item.get("lane", "")),
        namespace=str(item.get("namespace", "")),
        ts=str(item.get("ts", "")),
        alert_rule=str(item.get("alert_rule", "")),
        alert_hint=str(item.get("alert_hint", "")),
        evidence_source=str(item.get("evidence_source", "")),
        tenant_id=tenant_id,
        agent_id=agent_id,
        hostname=hostname,
        lane_hint=str(item.get("lane_hint", "")),
        lane_authoritative=bool(item.get("lane_authoritative", False)),
        stream_tags=tuple(item.get("stream_tags") or ()),
        signal_type=str(item.get("signal_type", "ANOMALY")),
    )


def to_agent_evidence_item(e: CanonicalEvidence) -> dict:
    """canonical -> VM lane EvidenceItem-shaped dict (agent_id/hostname/tenant_id
    excluded — those live on the wrapping AgentEvidenceRequest, not EvidenceItem)."""
    return {
        "trace_id": e.trace_id, "probe": e.probe, "result": e.result,
        "extracted_fact": e.extracted_fact, "raw": e.raw,
        "symptom_group": e.symptom_group, "lane": e.lane, "namespace": e.namespace,
        "ts": e.ts, "alert_rule": e.alert_rule, "alert_hint": e.alert_hint,
        "evidence_source": e.evidence_source, "lane_hint": e.lane_hint,
        "lane_authoritative": e.lane_authoritative, "stream_tags": list(e.stream_tags),
        "signal_type": e.signal_type,
    }
