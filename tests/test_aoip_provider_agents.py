"""Provider Agents projection — operator can see real remote agent liveness.

Product step fixed: Provider Portal /agents must not be a stub. It must read the
remote-agent registry and expose online/stale/offline state with operational
details the UI can render.
"""
from __future__ import annotations

import json
import time

import fakeredis.aioredis as aioredis
import httpx

from aoip.console import identity
from aoip.console.agents import build_provider_agents
from aoip.console.app import create_provider_app

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


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c")


def _auth(sid):
    return {"Authorization": f"Bearer {sid}"}


async def _seed_agent(r, agent_id: str, *, tenant: str, hostname: str, last_seen: int,
                      capabilities: list[str] | None = None, evidence_count: int = 0,
                      version: str = "1.1.3", bundle_sha256: str = "",
                      aoip_bundle_sha256: str = ""):
    await r.set(f"omni:remote_agent:registry:{agent_id}", json.dumps({
        "agent_id": agent_id,
        "tenant_id": tenant,
        "hostname": hostname,
        "version": version,
        "platform": "linux",
        "capabilities": capabilities or ["metrics", "discovery"],
        "last_seen": last_seen,
        "registered_at": last_seen - 60,
        "evidence_count": evidence_count,
        "bundle_sha256": bundle_sha256,
        "aoip_bundle_sha256": aoip_bundle_sha256,
    }))


async def test_provider_agents_projection_reads_real_registry_and_checks():
    r = _redis()
    await _seed_agent(r, "acme-edge", tenant="acme", hostname="cust-edge",
                      last_seen=990, evidence_count=7)
    await _seed_agent(r, "acme-app", tenant="acme", hostname="cust-app",
                      last_seen=850, evidence_count=3)
    await _seed_agent(r, "globex-db", tenant="globex", hostname="cust-db",
                      last_seen=0, capabilities=["metrics"], evidence_count=0)
    await r.hset("omni:remote_agent:checks:acme-edge", mapping={
        "disk_usage": json.dumps({"ts": "995", "result": "PASSED", "alert_hint": "disk ok"}),
    })
    await r.zadd("omni:cmd:ready:acme:acme-edge", {"cmd-1": 1000})

    result = await build_provider_agents(r, now=NOW)

    assert result["summary"] == {
        "total": 3, "online": 1, "stale": 1, "offline": 1, "drifted": 0,
    }
    by_id = {a["agent_id"]: a for a in result["agents"]}
    assert by_id["acme-edge"]["status"] == "online"
    assert by_id["acme-edge"]["discovery_enabled"] is True
    assert by_id["acme-edge"]["last_discovery_result"]["result"] == "PASSED"
    assert by_id["acme-edge"]["command_state"] == "active"
    assert by_id["acme-app"]["status"] == "stale"


async def test_provider_agents_projection_reports_drift_and_runtime():
    r = _redis()
    await r.set("omni:agent:release_manifest", json.dumps({
        "version": "1.2.0", "bundle_sha256": "cafe" * 16, "aoip_bundle_sha256": "beef" * 16,
    }))
    await _seed_agent(r, "acme-employee", tenant="acme", hostname="cust-app", last_seen=999,
                      version="1.2.0", bundle_sha256="cafe" * 16, aoip_bundle_sha256="beef" * 16)
    await _seed_agent(r, "acme-legacy-current", tenant="acme", hostname="cust-edge",
                      last_seen=999, version="1.2.0", bundle_sha256="cafe" * 16)
    await _seed_agent(r, "acme-drifted", tenant="acme", hostname="cust-db", last_seen=999,
                      version="1.1.3", bundle_sha256="stale" * 12)

    result = await build_provider_agents(r, now=NOW)

    assert result["summary"]["drifted"] == 1
    by_id = {a["agent_id"]: a for a in result["agents"]}
    assert by_id["acme-employee"]["runtime"] == "employee"
    assert by_id["acme-employee"]["drift_status"] == "current"
    assert by_id["acme-legacy-current"]["runtime"] == "legacy"
    assert by_id["acme-legacy-current"]["drift_status"] == "current"
    assert by_id["acme-drifted"]["drift_status"] == "drifted"


async def test_tenant_filter_is_applied_before_projection():
    r = _redis()
    await _seed_agent(r, "acme-edge", tenant="acme", hostname="cust-edge", last_seen=999)
    await _seed_agent(r, "globex-db", tenant="globex", hostname="globex-db", last_seen=999)
    result = await build_provider_agents(r, now=NOW, tenant_id="acme")
    assert [a["agent_id"] for a in result["agents"]] == ["acme-edge"]
    assert result["summary"]["total"] == 1


async def test_provider_agents_endpoint_enforces_provider_rbac():
    r = _redis()
    await _provision(r)
    await _seed_agent(r, "acme-edge", tenant="acme", hostname="cust-edge", last_seen=int(time.time()))
    prov = await _sid_provider(r)
    ten = await _sid_tenant(r)

    async with _client(create_provider_app(r)) as c:
        assert (await c.get("/api/provider/v1/agents")).status_code == 401
        assert (await c.get("/api/provider/v1/agents", headers=_auth(ten))).status_code == 403
        ok = await c.get("/api/provider/v1/agents", headers=_auth(prov))
        assert ok.status_code == 200
        assert ok.json()["agents"][0]["hostname"] == "cust-edge"


async def test_tenant_agents_endpoint_is_tenant_scoped():
    r = _redis(); await _provision(r)
    await _seed_agent(r, "acme-edge", tenant="acme", hostname="cust-edge", last_seen=999)
    await _seed_agent(r, "globex-db", tenant="globex", hostname="globex-db", last_seen=999)
    sid = await _sid_tenant(r)
    from aoip.console.app import create_tenant_app
    async with _client(create_tenant_app(r)) as c:
        resp = await c.get("/api/tenant/v1/agents", headers=_auth(sid))
    assert resp.status_code == 200
    assert [a["agent_id"] for a in resp.json()["agents"]] == ["acme-edge"]
