"""Provider Human Inbox — Unknown -> Question -> Answer -> Claim."""
from __future__ import annotations

import time

import fakeredis.aioredis as aioredis
import httpx

from aoip.competency_matrix import build_entity_competency
from aoip.console import identity
from aoip.console.app import create_provider_app
from aoip.console.human_inbox import build_provider_human_inbox
from aoip.objects import Fact
from aoip.question_lifecycle import sync_unknowns_from_competency
from aoip.claims_store import load_claims
from aoip.system_model import SystemModel

NOW = 1000.0


def _redis():
    return aioredis.FakeRedis(decode_responses=True)


async def _provision(r):
    await identity.upsert_user(r, subject="owner@aoip", email="owner@aoip")
    await identity.grant_provider_role(r, subject="owner@aoip", role="platform_owner")
    await identity.upsert_user(r, subject="sre@acme", email="sre@acme")
    await identity.add_membership(r, subject="sre@acme", tenant="acme", role="sre_lead")


async def _seed_unknown(r):
    model = SystemModel(scope="acme", facts=(
        Fact(subject="host:cust-edge", predicate="runs_service", obj="nginx",
             confidence=0.9, provenance=("discovery:t1", "agent:edge")),
    ))
    comp = build_entity_competency(model, [], entity_type="service", entity_id="svc:nginx", now=NOW)
    await sync_unknowns_from_competency(r, "acme", comp, now=NOW)


async def _sid_provider(r):
    p = await identity.resolve_provider_principal(r, "owner@aoip")
    return (await identity.issue_session(r, principal=p, now=time.time())).sid


async def _sid_tenant(r):
    p = await identity.resolve_tenant_principal(r, "sre@acme", "acme")
    return (await identity.issue_session(r, principal=p, now=time.time())).sid


def _auth(sid):
    return {"Authorization": f"Bearer {sid}"}


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c")


async def test_human_inbox_opens_questions_from_unknowns():
    r = _redis()
    await _seed_unknown(r)

    inbox = await build_provider_human_inbox(r, now=NOW)

    assert inbox["summary"]["pending_questions"] > 0
    q = inbox["tenants"][0]["questions"][0]
    assert q["status"] == "PENDING"
    assert q["can_create_claim"] is True
    assert q["text"]


async def test_human_inbox_endpoint_and_answer_create_claim():
    r = _redis()
    await _provision(r)
    await _seed_unknown(r)
    prov = await _sid_provider(r)
    ten = await _sid_tenant(r)

    async with _client(create_provider_app(r)) as c:
        assert (await c.get("/api/provider/v1/human-inbox")).status_code == 401
        assert (await c.get("/api/provider/v1/human-inbox", headers=_auth(ten))).status_code == 403

        inbox = (await c.get("/api/provider/v1/human-inbox", headers=_auth(prov))).json()
        q = next(q for q in inbox["tenants"][0]["questions"] if q["facet"] == "owner")
        resp = await c.post(
            f"/api/provider/v1/questions/{q['tenant_id']}/{q['question_id']}/answer",
            headers=_auth(prov),
            json={"value": "team-edge", "answered_by": "operator@aoip"},
        )
        assert resp.status_code == 200
        assert resp.json()["answer"]["value"] == "team-edge"
        claims = await load_claims(r, q["tenant_id"])
        assert any(c.value == "team-edge" for c in claims)
