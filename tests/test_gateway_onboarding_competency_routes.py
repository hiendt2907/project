"""Slice O2A/O2B: gateway read API for Competency Matrix + Unknown/Question/Answer."""
from __future__ import annotations

import json
from typing import Any

import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aoip.competency_matrix import build_entity_competency
from aoip.objects import Fact
from aoip.question_lifecycle import ensure_question_for_unknown, sync_unknowns_from_competency
from aoip.system_model import SystemModel
from aoip.system_model_store import fold_and_persist
from gateway.routes.onboarding import router


def _app(redis: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.redis = redis
    return app


def _redis() -> Any:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


class TestCompetencyEndpoint:
    @pytest.mark.asyncio
    async def test_get_competency_for_host(self):
        r = _redis()
        await fold_and_persist(
            r, "acme",
            [Fact(subject="host:web-01", predicate="exposes_port", obj="80", confidence=0.9, provenance=("discovery:port_scan:t1", "agent:a1"))],
            source="s1",
        )
        async with AsyncClient(transport=ASGITransport(app=_app(r)), base_url="http://test") as c:
            resp = await c.get("/onboarding/competency", params={"entity_type": "host", "entity_id": "host:web-01", "tenant_id": "acme"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["entity_id"] == "host:web-01"
        assert data["facets"]["listening_ports"]["state"] == "VERIFIED"
        assert "coverage_pct" in data["coverage"]

    @pytest.mark.asyncio
    async def test_invalid_entity_type_rejected(self):
        r = _redis()
        async with AsyncClient(transport=ASGITransport(app=_app(r)), base_url="http://test") as c:
            resp = await c.get("/onboarding/competency", params={"entity_type": "database", "entity_id": "db:x", "tenant_id": "acme"})
        assert resp.status_code == 422


class TestUnknownsAndQuestionsEndpoints:
    @pytest.mark.asyncio
    async def test_list_unknowns_and_questions_then_answer(self):
        r = _redis()
        model = SystemModel(scope="acme", facts=(
            Fact(subject="host:web-01", predicate="runs_service", obj="payment-api", confidence=0.9, provenance=("discovery:a", "agent:a1")),
        ))
        comp = build_entity_competency(model, [], entity_type="service", entity_id="svc:payment-api", now=1000.0)
        touched = await sync_unknowns_from_competency(r, "acme", comp, now=1000.0)
        owner_unknown = next(u for u in touched if u["facet"] == "owner")
        await ensure_question_for_unknown(r, "acme", owner_unknown, now=1000.0)

        async with AsyncClient(transport=ASGITransport(app=_app(r)), base_url="http://test") as c:
            resp_u = await c.get("/onboarding/unknowns", params={"tenant_id": "acme"})
            assert resp_u.status_code == 200
            assert any(u["facet"] == "owner" for u in resp_u.json()["unknowns"])

            resp_q = await c.get("/onboarding/questions", params={"tenant_id": "acme"})
            assert resp_q.status_code == 200
            questions = resp_q.json()["questions"]
            owner_question = next(q for q in questions if q["facet"] == "owner")
            assert owner_question["status"] == "PENDING"

            resp_a = await c.post(
                f"/onboarding/questions/{owner_question['question_id']}/answer",
                json={"answered_by": "alice", "value": "team-payments", "tenant_id": "acme"},
            )
            assert resp_a.status_code == 200
            assert resp_a.json()["answer"]["answered_by"] == "alice"

            # Answering twice fails (not PENDING anymore).
            resp_a2 = await c.post(
                f"/onboarding/questions/{owner_question['question_id']}/answer",
                json={"answered_by": "bob", "value": "team-x", "tenant_id": "acme"},
            )
            assert resp_a2.status_code == 404

    @pytest.mark.asyncio
    async def test_answer_unknown_question_id_404(self):
        r = _redis()
        async with AsyncClient(transport=ASGITransport(app=_app(r)), base_url="http://test") as c:
            resp = await c.post(
                "/onboarding/questions/does-not-exist/answer",
                json={"answered_by": "alice", "value": "x", "tenant_id": "acme"},
            )
        assert resp.status_code == 404


class TestEntitiesEndpoint:
    @pytest.mark.asyncio
    async def test_empty_twin_returns_empty_lists_revision_zero(self):
        r = _redis()
        async with AsyncClient(transport=ASGITransport(app=_app(r)), base_url="http://test") as c:
            resp = await c.get("/onboarding/entities", params={"tenant_id": "acme"})
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"tenant_id": "acme", "revision": 0, "hosts": [], "services": []}

    @pytest.mark.asyncio
    async def test_lists_hosts_and_services_from_twin(self):
        r = _redis()
        await fold_and_persist(
            r, "acme",
            [
                Fact(subject="host:web-01", predicate="exposes_port", obj="80", confidence=0.9, provenance=("discovery:port_scan:t1", "agent:a1")),
                Fact(subject="host:db-01", predicate="runs_service", obj="mariadb", confidence=0.9, provenance=("discovery:process_list:t2", "agent:a2")),
                Fact(subject="host:web-01", predicate="runs_service", obj="nginx", confidence=0.9, provenance=("discovery:process_list:t1", "agent:a1")),
            ],
            source="s1",
        )
        async with AsyncClient(transport=ASGITransport(app=_app(r)), base_url="http://test") as c:
            resp = await c.get("/onboarding/entities", params={"tenant_id": "acme"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["revision"] == 1
        assert data["hosts"] == ["host:db-01", "host:web-01"]
        assert data["services"] == ["svc:mariadb", "svc:nginx"]

    @pytest.mark.asyncio
    async def test_tenant_isolation_other_tenant_empty(self):
        r = _redis()
        await fold_and_persist(
            r, "acme",
            [Fact(subject="host:web-01", predicate="exposes_port", obj="80", confidence=0.9, provenance=("discovery:port_scan:t1", "agent:a1"))],
            source="s1",
        )
        async with AsyncClient(transport=ASGITransport(app=_app(r)), base_url="http://test") as c:
            resp = await c.get("/onboarding/entities", params={"tenant_id": "other"})
        assert resp.status_code == 200
        assert resp.json()["hosts"] == []


class TestSystemTwinEndpoint:
    @pytest.mark.asyncio
    async def test_system_twin_exposes_operational_host_service_port_projection(self):
        r = _redis()
        await fold_and_persist(
            r, "acme", [
                Fact(subject="host:edge", predicate="runs_service", obj="nginx", confidence=0.9,
                     provenance=("discovery:port_scan:t1", "agent:a1")),
                Fact(subject="host:edge", predicate="hosts", obj="svc:nginx", confidence=0.9,
                     provenance=("discovery:port_scan:t1", "agent:a1")),
                Fact(subject="host:edge", predicate="exposes_port", obj="80", confidence=0.9,
                     provenance=("discovery:port_scan:t1", "agent:a1")),
                Fact(subject="host:edge", predicate="connects_to", obj="host:app", confidence=0.7,
                     provenance=("discovery:connection_scan:t2", "agent:a1")),
            ], source="discovery",
        )
        await r.hset(
            "omni:onboarding:doc:acme", "port_scan@edge",
            '{"hostname":"edge","listening_ports":[{"port":80,"service":"nginx"}]}',
        )
        async with AsyncClient(transport=ASGITransport(app=_app(r)), base_url="http://test") as c:
            data = (await c.get("/onboarding/system-twin", params={"tenant_id": "acme"})).json()

        assert data["operational_hosts"] == [{
            "host": "host:edge",
            "ports": ["80"],
            "services": [{
                "name": "nginx", "ports": ["80"], "confidence": 0.9,
                "provenance": ["discovery:port_scan:t1", "agent:a1"],
            }],
            "connections": [{
                "target": "host:app", "confidence": 0.7,
                "provenance": ["discovery:connection_scan:t2", "agent:a1"],
            }],
        }]

    @pytest.mark.asyncio
    async def test_system_twin_exposes_only_redacted_api_sequence_metadata(self):
        r = _redis()
        await r.hset(
            "omni:onboarding:doc:acme", "api_access@edge",
            '{"hostname":"edge","api_interactions":[{"method":"GET","route":"/api/orders/:id","status_class":"2xx","count":3,"upstream":"app:8080","source_path":"/var/log/nginx/access.log"}]}',
        )
        await r.hset(
            "omni:onboarding:doc:acme", "api_contract@edge",
            '{"hostname":"edge","api_contracts":[{"path":"/app/openapi.json","format":"openapi","version":"3.0.0","routes":[{"method":"GET","route":"/api/orders/:id","operation_id":"getOrder","tags":["orders"],"response_statuses":["200"]}]}]}',
        )
        async with AsyncClient(transport=ASGITransport(app=_app(r)), base_url="http://test") as c:
            data = (await c.get("/onboarding/system-twin", params={"tenant_id": "acme"})).json()

        assert data["api_sequence"]["status"] == "runtime_verified"
        assert data["api_sequence"]["interactions"] == [{
            "source_host": "host:edge", "target_host": "app:8080", "method": "GET",
            "route": "/api/orders/:id", "operation_id": "getOrder", "status_class": "2xx", "count": 3,
            "runtime_observed": True, "confidence": 0.95,
            "provenance": "api_contract:/app/openapi.json",
        }]

    @pytest.mark.asyncio
    async def test_api_sequence_matches_openapi_base_path_and_parameter_template(self):
        r = _redis()
        await r.hset("omni:onboarding:doc:acme", "api_access@edge", json.dumps({
            "api_interactions": [{"method": "GET", "route": "/api/orders/:id", "status_class": "2xx", "count": 1}],
        }))
        await r.hset("omni:onboarding:doc:acme", "api_contract@edge", json.dumps({
            "api_contracts": [{"path": "openapi.json", "base_path": "/api", "routes": [{"method": "GET", "route": "/orders/{id}", "operation_id": "getOrder"}]}],
        }))
        async with AsyncClient(transport=ASGITransport(app=_app(r)), base_url="http://test") as c:
            data = (await c.get("/onboarding/system-twin", params={"tenant_id": "acme"})).json()
        assert data["api_sequence"]["status"] == "runtime_verified"
        assert data["api_sequence"]["interactions"][0]["runtime_observed"] is True

    @pytest.mark.asyncio
    async def test_system_twin_aggregate_exposes_revision_graph_unknowns_and_contradictions(self):
        r = _redis()
        facts = [
            Fact(subject="host:web-01", predicate="runs_service", obj="payments", confidence=0.9,
                 provenance=("discovery:service_topology:t1", "agent:a1")),
            Fact(subject="host:web-01", predicate="connects_to", obj="db:payments", confidence=0.8,
                 provenance=("discovery:connection_scan:t1", "agent:a1")),
        ]
        await fold_and_persist(r, "acme", facts, source="discovery")

        async with AsyncClient(transport=ASGITransport(app=_app(r)), base_url="http://test") as c:
            resp = await c.get("/onboarding/system-twin", params={"tenant_id": "acme"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["tenant_id"] == "acme"
        assert data["revision"] == 1
        assert data["summary"]["hosts"] == 1
        assert data["summary"]["services"] == 1
        assert data["summary"]["edges"] == 1
        assert data["summary"]["unknown_edge_targets"] == ["db:payments"]
        assert data["entities"]["hosts"] == ["host:web-01"]
        assert data["entities"]["services"] == ["svc:payments"]
        assert data["contradictions"] == []
        assert isinstance(data["unknowns"], list)

    @pytest.mark.asyncio
    async def test_system_twin_is_tenant_scoped(self):
        r = _redis()
        await fold_and_persist(
            r, "acme",
            [Fact(subject="host:private", predicate="exposes_port", obj="443", confidence=0.9,
                  provenance=("discovery:port_scan:t1", "agent:a1"))],
            source="discovery",
        )
        async with AsyncClient(transport=ASGITransport(app=_app(r)), base_url="http://test") as c:
            resp = await c.get("/onboarding/system-twin", params={"tenant_id": "other"})
        assert resp.status_code == 200
        assert resp.json()["revision"] == 0
        assert resp.json()["entities"] == {"hosts": [], "services": []}
