"""Sub-slice A — Provider Control Tower overview.

Chứng minh: Overview số THẬT từ Trace Spine + agent registry + component liveness; metric chưa
có nguồn → available=False + reason (không bịa số); endpoint enforce provider RBAC (401 unauth,
403 tenant-principal, 200 provider-viewer).
"""
from __future__ import annotations

import json
import time

import fakeredis.aioredis as aioredis
import httpx

from aoip.agent.intake import admit_command
from aoip.agent.trace import Correlation, RuntimeTrace, canonical_scope
from aoip.console import identity
from aoip.console.app import create_provider_app
from aoip.console.overview import build_provider_overview
from aoip.objects import ActionState
from aoip.recovery import Approval, RecoveryGate, RecoveryRequest, plan_recovery

NOW = 1000.0


def _redis():
    return aioredis.FakeRedis(decode_responses=True)


def _gate():
    return RecoveryGate(allowed_failure_modes=frozenset({"process_down"}),
                        allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                        scope_prefix="svc:", min_diagnosis_confidence=0.3, max_diagnosis_age_s=300.0,
                        allowed_targets=frozenset({"redis-server"}))


def _req(tenant="acme"):
    action = plan_recovery(failed_node="svc:db", failure_mode="process_down", substrate="systemd",
                           unit="redis-server", port=6379, risk=0.3).at(ActionState.APPROVED)
    return RecoveryRequest(failed_node="svc:db", failure_mode="process_down", substrate="systemd",
                           unit="redis-server", port=6379, action=action, risk=0.3,
                           diagnosed_at=NOW, dependents=(), tenant=tenant)


def _corr(tenant="acme", incident="inc-1"):
    return Correlation(tenant=tenant, agent_id="agent-1", mission_id="mis-1", incident_id=incident,
                       decision_id="dec-1", action_id="act-1", command_id="cmd-1",
                       canonical_scope=canonical_scope(tenant, "svc:db"))


def _approval(corr, req):
    return Approval.issue(approver="alice", tenant=corr.tenant, canonical_scope=corr.canonical_scope,
                          decision_goal=req.action.decision_goal, action_id=corr.action_id,
                          action_scope=req.action.scope, issued_at=NOW - 10, expires_at=NOW + 300)


async def _seed_incident(r, tenant="acme", incident="inc-1"):
    corr, req = _corr(tenant, incident), _req(tenant)
    await admit_command(corr=corr, req=req, gate=_gate(), approval=_approval(corr, req), now=NOW,
                        redis=r, holder="agent-1", trace=RuntimeTrace(r))
    return corr


async def _seed_agent(r, agent_id, *, last_seen, tenant="acme"):
    await r.set(f"omni:remote_agent:registry:{agent_id}",
                json.dumps({"agent_id": agent_id, "tenant_id": tenant, "last_seen": last_seen}))


async def _provision(r):
    await identity.upsert_user(r, subject="owner@aoip", email="owner@aoip")
    await identity.grant_provider_role(r, subject="owner@aoip", role="platform_owner")
    await identity.upsert_user(r, subject="sre@acme", email="sre@acme")
    await identity.add_membership(r, subject="sre@acme", tenant="acme", role="sre_lead")


async def _sid_provider(r, subject):
    p = await identity.resolve_provider_principal(r, subject)
    return (await identity.issue_session(r, principal=p, now=time.time())).sid


async def _sid_tenant(r, subject, tenant):
    p = await identity.resolve_tenant_principal(r, subject, tenant)
    return (await identity.issue_session(r, principal=p, now=time.time())).sid


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c")


def _auth(sid):
    return {"Authorization": f"Bearer {sid}"}


# ── aggregation thuần: số thật + khe hở nêu rõ ───────────────────────────────
async def test_overview_counts_real_incidents_and_agents():
    r = _redis()
    await _seed_incident(r)
    now = int(time.time())
    await _seed_agent(r, "agent-online", last_seen=now)
    await _seed_agent(r, "agent-stale", last_seen=now - 999)

    ov = await build_provider_overview(r, None, RuntimeTrace(r), now=float(now))

    assert ov["agents"]["available"] is True
    assert ov["agents"]["value"] == {"online": 1, "offline": 1, "total": 2}
    # incident vừa admit chưa terminal → active; xuất hiện trong recent_activity
    assert ov["active_incidents"]["value"] >= 1
    assert ov["recent_activity"]["available"] is True
    assert any(e["tenant"] == "acme" for e in ov["recent_activity"]["value"])


async def test_overview_marks_missing_sources_unavailable_with_reason():
    r = _redis()
    ov = await build_provider_overview(r, None, RuntimeTrace(r), now=NOW)
    for key in ("tenants_onboarding", "missions", "pending_questions"):
        assert ov[key]["available"] is False
        assert ov[key]["reason"]  # phải nêu khe hở nguồn, không rỗng
    # pool None → tenants cũng unavailable (cần Postgres)
    assert ov["tenants"]["available"] is False
    # governing rule: KHÔNG có metric product-domain thiếu backend
    assert "license_warnings" not in ov
    assert "agent_version_drift" not in ov


async def test_component_health_reports_redis_ok_pg_unavailable():
    r = _redis()
    ov = await build_provider_overview(r, None, RuntimeTrace(r), now=NOW)
    comps = {c["name"]: c["status"] for c in ov["component_health"]["value"]}
    assert comps["redis"] == "ok"
    assert comps["postgres"] == "unavailable"


async def test_tenants_rollup_from_fake_pool():
    """PG path: build_provider_overview đọc omni_admin.tenant GROUP BY status."""
    r = _redis()

    class _Conn:
        async def fetch(self, _sql):
            return [{"status": "active", "n": 3}, {"status": "suspended", "n": 1}]

        async def execute(self, _sql):
            return "SELECT 1"

    class _Acq:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def acquire(self):
            return _Acq()

    ov = await build_provider_overview(r, _Pool(), RuntimeTrace(r), now=NOW)
    assert ov["tenants"]["value"] == {"total": 4, "active": 3, "suspended": 1}
    comps = {c["name"]: c["status"] for c in ov["component_health"]["value"]}
    assert comps["postgres"] == "ok"


# ── endpoint RBAC ────────────────────────────────────────────────────────────
async def test_overview_endpoint_enforces_provider_rbac():
    r = _redis(); await _provision(r)
    prov = await _sid_provider(r, "owner@aoip")
    ten = await _sid_tenant(r, "sre@acme", "acme")
    async with _client(create_provider_app(r)) as c:
        assert (await c.get("/api/provider/v1/overview")).status_code == 401
        assert (await c.get("/api/provider/v1/overview", headers=_auth(ten))).status_code == 403
        ok = await c.get("/api/provider/v1/overview", headers=_auth(prov))
        assert ok.status_code == 200
        body = ok.json()
        assert "agents" in body and "active_incidents" in body and "component_health" in body
