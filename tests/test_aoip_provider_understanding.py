"""Provider Understanding projection — System Twin visible in portal."""
from __future__ import annotations

import json
import time

import fakeredis.aioredis as aioredis
import httpx

from aoip.console import identity
from aoip.console.app import create_provider_app
from aoip.console.understanding import build_provider_understanding
from aoip.objects import Fact
from aoip.question_lifecycle import sync_unknowns_from_competency
from aoip.competency_matrix import build_entity_competency
from aoip.system_model import SystemModel
from aoip.system_model_store import fold_and_persist

NOW = 1000.0


def _redis():
    return aioredis.FakeRedis(decode_responses=True)


async def _provision(r):
    await identity.upsert_user(r, subject="owner@aoip", email="owner@aoip")
    await identity.grant_provider_role(r, subject="owner@aoip", role="platform_owner")
    await identity.upsert_user(r, subject="sre@acme", email="sre@acme")
    await identity.add_membership(r, subject="sre@acme", tenant="acme", role="sre_lead")


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


async def _seed_twin(r):
    facts = [
        Fact(subject="host:cust-edge", predicate="runs_service", obj="nginx",
             confidence=0.9, provenance=("discovery:process_list:t1", "agent:edge")),
        Fact(subject="host:cust-edge", predicate="exposes_port", obj="80",
             confidence=0.9, provenance=("discovery:port_scan:t1", "agent:edge")),
        Fact(subject="svc:nginx", predicate="depends_on", obj="svc:payment-api",
             confidence=0.7, provenance=("discovery:service_topology:t1", "agent:edge")),
    ]
    await fold_and_persist(r, "acme", facts, source="test")
    model = SystemModel(scope="acme", facts=tuple(facts))
    comp = build_entity_competency(model, [], entity_type="service", entity_id="svc:nginx", now=NOW)
    await sync_unknowns_from_competency(r, "acme", comp, now=NOW)
    await r.lpush("omni:aoip:contradictions:acme", json.dumps({
        "subject": "host:cust-edge",
        "predicate": "exposes_port",
        "existing_obj": "80",
        "incoming_obj": "8080",
        "detected_at": NOW,
    }))


async def test_understanding_projection_exposes_twin_unknowns_and_competency():
    r = _redis()
    await _seed_twin(r)

    result = await build_provider_understanding(r, now=NOW)

    tenant = result["tenants"][0]
    assert tenant["tenant_id"] == "acme"
    assert tenant["twin"]["revision"] == 1
    assert tenant["twin"]["entity_count"] >= 2
    assert tenant["twin"]["fact_count"] == 3
    assert tenant["facts"][0]["provenance"]
    assert tenant["unknown_count"] >= 1
    assert tenant["contradiction_count"] == 1
    assert tenant["competency"][0]["coverage"]["coverage_pct"] >= 0


async def test_understanding_endpoint_enforces_provider_rbac():
    r = _redis()
    await _provision(r)
    await _seed_twin(r)
    prov = await _sid_provider(r)
    ten = await _sid_tenant(r)

    async with _client(create_provider_app(r)) as c:
        assert (await c.get("/api/provider/v1/understanding")).status_code == 401
        assert (await c.get("/api/provider/v1/understanding", headers=_auth(ten))).status_code == 403
        ok = await c.get("/api/provider/v1/understanding", headers=_auth(prov))
        assert ok.status_code == 200
        assert ok.json()["tenants"][0]["tenant_id"] == "acme"


async def test_tenant_understanding_endpoint_returns_only_membership_scope():
    r = _redis(); await _provision(r); await _seed_twin(r)
    await fold_and_persist(r, "globex", [Fact(subject="host:globex", predicate="runs_service",
                                               obj="api", confidence=0.9, provenance=("test",))],
                           source="test")
    sid = await _sid_tenant(r)
    from aoip.console.app import create_tenant_app
    async with _client(create_tenant_app(r)) as c:
        resp = await c.get("/api/tenant/v1/understanding", headers=_auth(sid))
    assert resp.status_code == 200
    assert [t["tenant_id"] for t in resp.json()["tenants"]] == ["acme"]
