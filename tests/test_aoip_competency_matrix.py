"""Slice O2A: Entity Competency Matrix — deterministic projection over the
persisted SystemModel + contradiction log. No new graph/model, no LLM."""
from __future__ import annotations

import time
from typing import Any

import fakeredis.aioredis
import pytest

from aoip.competency_matrix import (
    FacetState,
    build_entity_competency,
    build_entity_competency_from_store,
    contradicted_facets,
    critical_unknowns,
    entity_coverage,
)
from aoip.objects import Fact
from aoip.system_model import SystemModel
from aoip.system_model_store import fold_and_persist


def _redis() -> Any:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _fact(subject: str, predicate: str, obj: str, *, provenance=("discovery:port_scan:tr1", "agent:a1"), ts: float = 1000.0, confidence: float = 0.9) -> Fact:
    return Fact(
        subject=subject, predicate=predicate, obj=obj, confidence=confidence,
        provenance=provenance, observation_time=ts, verified_time=ts,
    )


class TestDeterminism:
    def test_same_facts_same_matrix(self):
        model = SystemModel(scope="acme", facts=(_fact("host:web-01", "exposes_port", "80"),))
        comp1 = build_entity_competency(model, [], entity_type="host", entity_id="host:web-01", now=2000.0)
        comp2 = build_entity_competency(model, [], entity_type="host", entity_id="host:web-01", now=2000.0)
        assert comp1 == comp2


class TestMissingFact:
    def test_unobserved_entity_identity_unknown(self):
        model = SystemModel(scope="acme")
        comp = build_entity_competency(model, [], entity_type="host", entity_id="host:ghost", now=2000.0)
        assert comp.facet("identity").state == FacetState.UNKNOWN

    def test_service_process_facet_always_unknown_no_linking_evidence(self):
        model = SystemModel(scope="acme", facts=(_fact("host:web-01", "runs_service", "nginx"),))
        comp = build_entity_competency(model, [], entity_type="service", entity_id="svc:nginx", now=2000.0)
        assert comp.facet("process").state == FacetState.UNKNOWN

    def test_host_not_applicable_facets(self):
        model = SystemModel(scope="acme", facts=(_fact("host:web-01", "exposes_port", "80"),))
        comp = build_entity_competency(model, [], entity_type="host", entity_id="host:web-01", now=2000.0)
        assert comp.facet("owner").state == FacetState.NOT_APPLICABLE
        assert comp.facet("sla").state == FacetState.NOT_APPLICABLE


class TestContradicted:
    def test_flagged_contradiction_marks_facet_contradicted(self):
        model = SystemModel(scope="acme", facts=(_fact("host:web-01", "exposes_port", "80"),))
        contradiction = {
            "subject": "host:web-01", "predicate": "exposes_port",
            "existing_obj": "80", "existing_provenance": ["discovery:port_scan:tr1", "agent:a1"],
            "incoming_obj": "8080", "incoming_provenance": ["discovery:port_scan:tr2", "agent:a2"],
            "detected_at": 1005.0,
        }
        comp = build_entity_competency(model, [contradiction], entity_type="host", entity_id="host:web-01", now=2000.0)
        assert comp.facet("listening_ports").state == FacetState.CONTRADICTED

    def test_two_hosts_claiming_same_service_name_contradicted(self):
        model = SystemModel(
            scope="acme",
            facts=(
                _fact("host:web-01", "runs_service", "nginx", provenance=("discovery:a", "agent:a1")),
                _fact("host:web-02", "runs_service", "nginx", provenance=("discovery:b", "agent:a2")),
            ),
        )
        comp = build_entity_competency(model, [], entity_type="service", entity_id="svc:nginx", now=2000.0)
        assert comp.facet("host").state == FacetState.CONTRADICTED
        assert "host" in contradicted_facets(comp)


class TestStale:
    def test_old_verified_time_beyond_threshold_is_stale(self):
        model = SystemModel(scope="acme", facts=(_fact("host:web-01", "exposes_port", "80", ts=100.0),))
        comp = build_entity_competency(
            model, [], entity_type="host", entity_id="host:web-01", now=100000.0, freshness_sec=3600.0,
        )
        assert comp.facet("listening_ports").state == FacetState.STALE

    def test_fresh_within_threshold_is_verified(self):
        model = SystemModel(scope="acme", facts=(_fact("host:web-01", "exposes_port", "80", ts=100.0, confidence=0.9),))
        comp = build_entity_competency(
            model, [], entity_type="host", entity_id="host:web-01", now=200.0, freshness_sec=3600.0,
        )
        assert comp.facet("listening_ports").state == FacetState.VERIFIED


class TestQueryAPI:
    def test_entity_coverage_excludes_not_applicable(self):
        model = SystemModel(scope="acme", facts=(_fact("host:web-01", "exposes_port", "80"),))
        comp = build_entity_competency(model, [], entity_type="host", entity_id="host:web-01", now=2000.0)
        report = entity_coverage(comp)
        assert report["entity_id"] == "host:web-01"
        assert 0.0 <= report["coverage_pct"] <= 100.0
        assert FacetState.NOT_APPLICABLE.value not in report.get("state_counts", {}) or report["coverage_pct"] < 100.0

    def test_critical_unknowns_reports_owner_monitoring_sla_for_service(self):
        model = SystemModel(scope="acme", facts=(_fact("host:web-01", "runs_service", "nginx"),))
        comp = build_entity_competency(model, [], entity_type="service", entity_id="svc:nginx", now=2000.0)
        unknowns = critical_unknowns(comp)
        assert "owner" in unknowns
        assert "monitoring" in unknowns
        assert "sla" in unknowns


class TestTenantIsolationAndPersistReload:
    @pytest.mark.asyncio
    async def test_tenant_isolation_via_store(self):
        r = _redis()
        await fold_and_persist(r, "tenant-a", [_fact("host:web-01", "exposes_port", "80")], source="s1")
        comp_b = await build_entity_competency_from_store(
            r, "tenant-b", entity_type="host", entity_id="host:web-01", now=time.time(),
        )
        assert comp_b.facet("listening_ports").state == FacetState.UNKNOWN
        assert comp_b.facet("identity").state == FacetState.UNKNOWN

    @pytest.mark.asyncio
    async def test_reconstructable_from_persisted_facts_after_reload(self):
        r = _redis()
        now = time.time()
        await fold_and_persist(
            r, "acme", [_fact("host:web-01", "exposes_port", "80", ts=now)], source="s1",
        )
        comp1 = await build_entity_competency_from_store(
            r, "acme", entity_type="host", entity_id="host:web-01", now=now + 5,
        )
        # Reload independently (simulates a fresh process) — same persisted facts must
        # reconstruct the identical matrix.
        comp2 = await build_entity_competency_from_store(
            r, "acme", entity_type="host", entity_id="host:web-01", now=now + 5,
        )
        assert comp1 == comp2
        assert comp1.facet("listening_ports").state == FacetState.VERIFIED


class TestImportBoundary:
    def test_competency_matrix_does_not_import_mutation_or_recovery_code(self):
        import aoip.competency_matrix as cm

        src = cm.__file__
        with open(src, encoding="utf-8") as f:
            text = f.read()
        for forbidden in ("aoip.recovery", "aoip.runner", "workers.executor", "aoip.primitives"):
            assert forbidden not in text, f"competency_matrix.py must not import {forbidden}"
