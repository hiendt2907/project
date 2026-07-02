"""M2 Product loop — provider lab incident/proposal entrypoint.

First broken link in runtime journey: provider portal has no product API to create
or list a lab incident with persisted diagnosis + proposed typed action. This
slice must stop before command enqueue: approval is a separate next link.
"""
from __future__ import annotations

import json
import time

import fakeredis.aioredis as aioredis
import httpx

from aoip.agent.trace import RuntimeTrace
from aoip.console import identity
from aoip.console.app import create_provider_app


def _redis():
    return aioredis.FakeRedis(decode_responses=True)


async def _provision(r):
    await identity.upsert_user(r, subject="owner@aoip", email="owner@aoip")
    await identity.grant_provider_role(r, subject="owner@aoip", role="platform_owner")
    await identity.upsert_user(r, subject="sre@acme", email="sre@acme")
    await identity.add_membership(r, subject="sre@acme", tenant="staging-sim", role="sre_lead")


async def _sid_provider(r):
    p = await identity.resolve_provider_principal(r, "owner@aoip")
    return (await identity.issue_session(r, principal=p, now=time.time())).sid


async def _sid_tenant(r):
    p = await identity.resolve_tenant_principal(r, "sre@acme", "staging-sim")
    return (await identity.issue_session(r, principal=p, now=time.time())).sid


def _auth(sid):
    return {"Authorization": f"Bearer {sid}"}


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c")


async def _seed_agent(r, *, capabilities=None):
    await r.set("omni:remote_agent:registry:staging-sim_cust-edge", json.dumps({
        "agent_id": "staging-sim_cust-edge",
        "tenant_id": "staging-sim",
        "hostname": "cust-edge",
        "last_seen": int(time.time()),
        "capabilities": capabilities or ["metrics", "logs", "discovery", "systemd.restart_unit"],
    }))
    await r.hset("omni:remote_agent:checks:staging-sim_cust-edge", "service_nginx", json.dumps({
        "ts": str(int(time.time())),
        "result": "PASSED",
        "alert_hint": "nginx.service is active",
        "namespace": "cust-edge",
    }))


async def test_provider_can_create_lab_incident_with_typed_action_pending_approval():
    r = _redis()
    await _provision(r)
    await _seed_agent(r)
    sid = await _sid_provider(r)

    async with _client(create_provider_app(r)) as c:
        resp = await c.post("/api/provider/v1/lab/incidents", headers=_auth(sid), json={
            "tenant_id": "staging-sim",
            "agent_id": "staging-sim_cust-edge",
            "host": "cust-edge",
            "service": "nginx",
            "unit": "nginx.service",
        })
        assert resp.status_code == 200
        body = resp.json()

        assert body["status"] == "PENDING_APPROVAL"
        assert body["diagnosis"]["evidence_refs"]
        action = body["proposed_action"]
        assert action["operation_type"] == "systemd.restart_unit"
        assert action["tenant_id"] == "staging-sim"
        assert action["agent_id"] == "staging-sim_cust-edge"
        assert action["target"] == {"unit": "nginx.service"}
        assert action["expected_precondition"] == {"unit_exists": True}
        assert action["verification"] == {"active_state": "active", "sub_state": "running"}
        assert action["idempotency_key"]
        assert action["approval_id"]

        # First slice must not mutate or enqueue durable command before approval.
        assert await r.zcard("omni:cmd:ready:staging-sim:staging-sim_cust-edge") == 0
        pending = await RuntimeTrace(r).pending_approvals("staging-sim")
        assert len(pending) == 1

        listed = (await c.get("/api/provider/v1/incidents", headers=_auth(sid))).json()
        assert any(i["correlation_id"] == body["correlation_id"] for i in listed["incidents"])


async def test_lab_incident_endpoint_enforces_provider_rbac_and_capability_truth():
    r = _redis()
    await _provision(r)
    await _seed_agent(r, capabilities=["metrics", "logs"])
    prov = await _sid_provider(r)
    ten = await _sid_tenant(r)

    async with _client(create_provider_app(r)) as c:
        assert (await c.post("/api/provider/v1/lab/incidents", json={})).status_code == 401
        assert (await c.post("/api/provider/v1/lab/incidents", headers=_auth(ten), json={})).status_code == 403

        resp = await c.post("/api/provider/v1/lab/incidents", headers=_auth(prov), json={
            "tenant_id": "staging-sim",
            "agent_id": "staging-sim_cust-edge",
            "host": "cust-edge",
            "service": "nginx",
            "unit": "nginx.service",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "BLOCKED_AGENT_CAPABILITY"
        assert body["proposed_action"]["operation_type"] == "systemd.restart_unit"
        assert "systemd.restart_unit" in body["block_reason"]
