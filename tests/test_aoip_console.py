"""Slice 0 — hai portal production, danh tính SERVER-SIDE (session + membership).

Chứng minh: session opaque server-side; Principal từ membership KHÔNG từ client; namespace
/v1 enforce theo kind; tenant KHÔNG query được tenant khác; raw evidence gated + audited;
logout/revocation thu hồi session; role bị gỡ → session vô hiệu ngay.
"""
from __future__ import annotations

import time

import fakeredis.aioredis as aioredis
import httpx

from aoip.agent.intake import admit_command
from aoip.agent.trace import Correlation, RuntimeTrace, canonical_scope
from aoip.console import identity
from aoip.console.app import create_provider_app, create_tenant_app
from aoip.console.authz import KIND_PROVIDER, KIND_TENANT, Principal
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


async def _seed(r, tenant="acme", incident="inc-1"):
    corr, req = _corr(tenant, incident), _req(tenant)
    await admit_command(corr=corr, req=req, gate=_gate(), approval=_approval(corr, req), now=NOW,
                        redis=r, holder="agent-1", trace=RuntimeTrace(r))
    return corr


async def _provision(r):
    """Seed portal users (thay migration PG cho unit test)."""
    await identity.upsert_user(r, subject="owner@aoip", email="owner@aoip")
    await identity.grant_provider_role(r, subject="owner@aoip", role="platform_owner")
    await identity.upsert_user(r, subject="view@aoip", email="view@aoip")
    await identity.grant_provider_role(r, subject="view@aoip", role="provider_viewer")
    await identity.upsert_user(r, subject="sre@acme", email="sre@acme")
    await identity.add_membership(r, subject="sre@acme", tenant="acme", role="sre_lead")
    await identity.upsert_user(r, subject="sre@globex", email="sre@globex")
    await identity.add_membership(r, subject="sre@globex", tenant="globex", role="sre_lead")


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


# ── namespace + kind enforcement ─────────────────────────────────────────────
async def test_provider_endpoint_rejects_tenant_principal():
    r = _redis(); await _provision(r)
    sid = await _sid_tenant(r, "sre@acme", "acme")
    async with _client(create_provider_app(r)) as c:
        assert (await c.get("/api/provider/v1/tenants", headers=_auth(sid))).status_code == 403
        assert (await c.get("/api/provider/v1/tenants")).status_code == 401  # unauth


async def test_provider_can_create_tenant_only_with_change_policy(monkeypatch):
    r = _redis(); await _provision(r)
    from services.admin_config.repo import AdminConfigRepo

    async def fake_create(self, *, tenant_id, display_name, actor, idempotent=False):
        assert actor == "owner@aoip"
        return {"tenant_id": tenant_id, "display_name": display_name}

    monkeypatch.setattr(AdminConfigRepo, "create_tenant", fake_create)
    owner = await _sid_provider(r, "owner@aoip")
    viewer = await _sid_provider(r, "view@aoip")
    app = create_provider_app(r); app.state.pool = object()
    async with _client(app) as c:
        denied = await c.post("/api/provider/v1/tenants", headers=_auth(viewer),
                              json={"tenant_id": "new", "display_name": "New"})
        created = await c.post("/api/provider/v1/tenants", headers=_auth(owner),
                               json={"tenant_id": "new", "display_name": "New"})
    assert denied.status_code == 403
    assert created.status_code == 200
    assert created.json()["tenant_id"] == "new"


async def test_provider_autonomy_control_is_tenant_scoped_and_confirmed(monkeypatch):
    r = _redis(); await _provision(r)
    from services.admin_config.repo import AdminConfigRepo

    async def fake_get_tier(self, tenant_id):
        return "shadow"

    async def fake_set_tier(self, **kwargs):
        assert kwargs["tenant_id"] == "acme"
        return {"tier": kwargs["tier"], "version": 2, "dedup_key": "tier:acme:2"}

    monkeypatch.setattr(AdminConfigRepo, "get_tier", fake_get_tier)
    monkeypatch.setattr(AdminConfigRepo, "set_tier", fake_set_tier)
    owner = await _sid_provider(r, "owner@aoip")
    app = create_provider_app(r); app.state.pool = object()
    async with _client(app) as c:
        blocked = await c.post("/api/provider/v1/tenants/acme/autonomy",
                               headers=_auth(owner), json={"tier": "assist"})
        changed = await c.post("/api/provider/v1/tenants/acme/autonomy",
                               headers=_auth(owner), json={"tier": "assist", "confirm": True})
    assert blocked.status_code == 409
    assert changed.status_code == 200 and changed.json()["to"] == "assist"


async def test_provider_plan_projection_and_write_are_rbac_scoped(monkeypatch):
    r = _redis(); await _provision(r)
    from services.admin_config.repo import AdminConfigRepo

    async def fake_get_plan(self, tenant_id):
        return {"tenant_id": tenant_id, "plan_code": "standard", "agent_limit": 10,
                "autonomy_ceiling": "assist", "retention_days": 30,
                "support_tier": "standard", "enabled": True, "version": 1}

    async def fake_set_plan(self, **kwargs):
        assert kwargs["tenant_id"] == "acme"
        return {"tenant_id": "acme", "plan_code": kwargs["plan_code"], "version": 2}

    monkeypatch.setattr(AdminConfigRepo, "get_tenant_plan", fake_get_plan)
    monkeypatch.setattr(AdminConfigRepo, "set_tenant_plan", fake_set_plan)
    owner = await _sid_provider(r, "owner@aoip")
    viewer = await _sid_provider(r, "view@aoip")
    app = create_provider_app(r); app.state.pool = object()
    async with _client(app) as c:
        read = await c.get("/api/provider/v1/tenants/acme/plan", headers=_auth(viewer))
        denied = await c.post("/api/provider/v1/tenants/acme/plan", headers=_auth(viewer), json={
            "plan_code": "premium", "agent_limit": 50, "autonomy_ceiling": "auto",
            "retention_days": 90, "support_tier": "premium"})
        changed = await c.post("/api/provider/v1/tenants/acme/plan", headers=_auth(owner), json={
            "plan_code": "premium", "agent_limit": 50, "autonomy_ceiling": "auto",
            "retention_days": 90, "support_tier": "premium"})
    assert read.status_code == 200 and read.json()["autonomy_ceiling"] == "assist"
    assert denied.status_code == 403
    assert changed.status_code == 200 and changed.json()["plan_code"] == "premium"


async def test_tenant_endpoint_rejects_provider_principal():
    r = _redis(); await _provision(r)
    sid = await _sid_provider(r, "owner@aoip")
    async with _client(create_tenant_app(r)) as c:
        assert (await c.get("/api/tenant/v1/incidents", headers=_auth(sid))).status_code == 403


# ── danh tính từ session server-side, KHÔNG từ client ────────────────────────
async def test_me_reflects_server_side_membership():
    r = _redis(); await _provision(r)
    sid = await _sid_tenant(r, "sre@acme", "acme")
    async with _client(create_tenant_app(r)) as c:
        me = (await c.get("/api/tenant/v1/me", headers=_auth(sid))).json()
        assert me["active_tenant"] == "acme" and me["subject"] == "sre@acme"
        assert me["memberships"] == {"acme": "sre_lead"}


# ── same incident, two audience-appropriate projections ──────────────────────
async def test_same_incident_two_projections():
    r = _redis(); await _provision(r)
    corr = await _seed(r)
    ps = await _sid_provider(r, "owner@aoip"); ts = await _sid_tenant(r, "sre@acme", "acme")
    async with _client(create_provider_app(r)) as pc:
        pv = (await pc.get(f"/api/provider/v1/incident/acme/{corr.correlation_id}",
                           headers=_auth(ps))).json()["incident"]
    async with _client(create_tenant_app(r)) as tc:
        tv = (await tc.get(f"/api/tenant/v1/incident/{corr.correlation_id}",
                           headers=_auth(ts))).json()["incident"]
    assert pv["correlation_id"] == tv["correlation_id"] == corr.correlation_id
    assert "lease_token" in pv and "canonical_scope" in pv
    assert "lease_token" not in tv and "canonical_scope" not in tv
    assert "service" in tv and "explanation" in tv


# ── tenant KHÔNG query được tenant khác (identity từ server) ──────────────────
async def test_tenant_cannot_access_other_tenant():
    r = _redis(); await _provision(r)
    corr = await _seed(r, tenant="acme")
    gs = await _sid_tenant(r, "sre@globex", "globex")
    async with _client(create_tenant_app(r)) as c:
        resp = await c.get(f"/api/tenant/v1/incident/{corr.correlation_id}", headers=_auth(gs))
        assert resp.status_code in (403, 404)
        assert (await c.get("/api/tenant/v1/incidents", headers=_auth(gs))).json()["incidents"] == []


async def test_tenant_cannot_forge_membership():
    """globex user KHÔNG có membership acme → resolve acme = None (không thể tự khai)."""
    r = _redis(); await _provision(r)
    assert await identity.resolve_tenant_principal(r, "sre@globex", "acme") is None


# ── session lifecycle: logout + role-revocation ──────────────────────────────
async def test_logout_revokes_session():
    r = _redis(); await _provision(r)
    sid = await _sid_provider(r, "owner@aoip")
    async with _client(create_provider_app(r)) as c:
        assert (await c.get("/api/provider/v1/me", headers=_auth(sid))).status_code == 200
        assert (await c.post("/api/provider/v1/logout", headers=_auth(sid))).status_code == 200
        assert (await c.get("/api/provider/v1/me", headers=_auth(sid))).status_code == 401


async def test_role_revocation_invalidates_session():
    r = _redis(); await _provision(r)
    sid = await _sid_provider(r, "view@aoip")
    async with _client(create_provider_app(r)) as c:
        assert (await c.get("/api/provider/v1/me", headers=_auth(sid))).status_code == 200
        await r.delete(identity._PROVIDER_ROLES_K + "view@aoip")  # thu hồi role
        assert (await c.get("/api/provider/v1/me", headers=_auth(sid))).status_code == 401


async def test_disabled_user_cannot_resolve():
    r = _redis(); await _provision(r)
    await identity.upsert_user(r, subject="owner@aoip", email="owner@aoip", disabled=True)
    assert await identity.resolve_provider_principal(r, "owner@aoip") is None


# ── raw evidence: gated + audited ────────────────────────────────────────────
async def test_provider_raw_evidence_requires_permission_and_audits():
    r = _redis(); await _provision(r)
    corr = await _seed(r)
    view_sid = await _sid_provider(r, "view@aoip"); own_sid = await _sid_provider(r, "owner@aoip")
    async with _client(create_provider_app(r)) as c:
        assert (await c.get(f"/api/provider/v1/incident/acme/{corr.correlation_id}?raw=true",
                            headers=_auth(view_sid))).status_code == 403
        v = (await c.get(f"/api/provider/v1/incident/acme/{corr.correlation_id}",
                         headers=_auth(view_sid))).json()["incident"]
        assert "evidence" not in v and "evidence_redacted" in v
        ok = await c.get(f"/api/provider/v1/incident/acme/{corr.correlation_id}?raw=true",
                         headers=_auth(own_sid))
        assert ok.status_code == 200 and "evidence" in ok.json()["incident"]
        sessions = (await c.get("/api/provider/v1/support-access/acme",
                                headers=_auth(own_sid))).json()["sessions"]
        assert len(sessions) == 1 and sessions[0]["subject"] == "owner@aoip"


async def test_index_pages_distinct():
    r = _redis()
    async with _client(create_provider_app(r)) as c:
        assert "Provider Operations" in (await c.get("/")).text
    async with _client(create_tenant_app(r)) as c:
        assert "Your Operations" in (await c.get("/")).text
