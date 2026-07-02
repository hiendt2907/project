"""Slice O2A/O2B: gateway read API for Competency Matrix + Unknown/Question/Answer."""
from __future__ import annotations

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
