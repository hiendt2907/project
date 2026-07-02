"""DiscoveryEvidence envelope → Observation → Fact projection (Slice O1).

Additive migration only: turns the *existing* production discovery envelopes
(emitted by ``src/remote_agent/collectors/discovery_evidence.py``, scheduled by
the legacy ``src/remote_agent/agent.py`` startup+periodic loop — already
satisfies continuous discovery, no new scheduler needed) into canonical
``aoip.objects`` Observation/Fact, WITHOUT touching
``pkg.onboarding.discovery_doc`` (the legacy flat-Redis pipeline keeps running
unchanged alongside this).

Scope (Slice O1): only the 4 probes the legacy pipeline already produces —
process_list, port_scan, service_topology, doc_snapshot. INV_DATA_RESIDENCY:
doc_snapshot never becomes a Fact carrying raw content — only a content-hash
reference node, mirroring ``discovery_doc._sanitize_documents``.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from aoip.objects import Fact, Observation
from aoip.system_graph import make_node

SCHEMA_VERSION = 1

SUPPORTED_PROBES = frozenset({"process_list", "port_scan", "service_topology", "doc_snapshot"})

_CONFIDENCE_BY_PROBE = {
    "process_list": 0.6,
    "port_scan": 0.85,
    "service_topology": 0.85,
    "doc_snapshot": 1.0,
}


def _extract_discovery_data(ev_doc: dict[str, Any]) -> dict[str, Any] | None:
    fact = ev_doc.get("extracted_fact") or {}
    if isinstance(fact, str):
        try:
            fact = json.loads(fact)
        except Exception:
            fact = {}
    discovery_data = fact.get("discovery_data") if isinstance(fact, dict) else None
    return discovery_data if isinstance(discovery_data, dict) else None


def to_observation(
    ev_doc: dict[str, Any], *, tenant_id: str, agent_id: str, host: str,
) -> Observation | None:
    """Normalize one DiscoveryEvidence envelope into an Observation.

    Returns None for unsupported probes or malformed evidence (never raises —
    caller treats this as "nothing to project", legacy accumulation is
    unaffected either way).
    """
    probe = str(ev_doc.get("probe") or "unknown")
    if probe not in SUPPORTED_PROBES:
        return None
    discovery_data = _extract_discovery_data(ev_doc)
    if discovery_data is None:
        return None
    trace_id = str(ev_doc.get("trace_id") or "")
    payload = json.dumps(discovery_data, sort_keys=True, ensure_ascii=False)
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return Observation(
        source=f"discovery:{probe}",
        scope=f"{tenant_id}/{host}",
        data={
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "host": host,
            "probe": probe,
            "trace_id": trace_id,
            "content_hash": content_hash,
            "schema_version": SCHEMA_VERSION,
            "discovery_data": discovery_data,
        },
    )


def project_facts(observation: Observation) -> tuple[Fact, ...]:
    """Observation → Fact candidates. Deterministic — same Observation always
    yields the same Facts (required for fold() idempotency + testability).

    Only structural/mapping facts are produced (INV_DATA_RESIDENCY) — no
    narrative/business-purpose text (e.g. service descriptions, doc content)
    ever lands in a Fact.obj.
    """
    probe = observation.data["probe"]
    host = observation.data["host"]
    discovery_data = observation.data["discovery_data"]
    trace_id = observation.data["trace_id"]
    agent_id = observation.data["agent_id"]
    confidence = _CONFIDENCE_BY_PROBE.get(probe, 0.5)
    provenance = (f"discovery:{probe}:{trace_id}", f"agent:{agent_id}")
    host_node = make_node("host", host)
    ts = observation.ts

    facts: list[Fact] = []
    if probe == "process_list":
        for proc in discovery_data.get("processes") or []:
            name = str(proc.get("name") or "").strip()
            if not name:
                continue
            facts.append(
                Fact(
                    subject=host_node, predicate="runs_process", obj=name,
                    confidence=confidence, provenance=provenance,
                    observation_time=ts, verified_time=ts,
                )
            )
    elif probe == "port_scan":
        for p in discovery_data.get("listening_ports") or []:
            port = p.get("port")
            if port is None:
                continue
            facts.append(
                Fact(
                    subject=host_node, predicate="exposes_port", obj=str(port),
                    confidence=confidence, provenance=provenance,
                    observation_time=ts, verified_time=ts,
                )
            )
            service = str(p.get("service") or "").strip()
            if service:
                facts.append(
                    Fact(
                        subject=host_node, predicate="runs_service", obj=service,
                        confidence=confidence, provenance=provenance,
                        observation_time=ts, verified_time=ts,
                    )
                )
    elif probe == "service_topology":
        for svc in discovery_data.get("services") or []:
            name = str(svc.get("name") or "").strip()
            if not name:
                continue
            facts.append(
                Fact(
                    subject=host_node, predicate="runs_service", obj=name,
                    confidence=confidence, provenance=provenance,
                    observation_time=ts, verified_time=ts,
                )
            )
    elif probe == "doc_snapshot":
        for doc in discovery_data.get("documents") or []:
            content = str(doc.get("content") or "")
            doc_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
            doc_node = make_node("document", doc_hash)
            facts.append(
                Fact(
                    subject=doc_node, predicate="observed_from", obj=host_node,
                    confidence=confidence, provenance=provenance,
                    observation_time=ts, verified_time=ts,
                )
            )
    return tuple(facts)
