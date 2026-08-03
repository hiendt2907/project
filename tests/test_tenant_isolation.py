"""TDD: multi-tenant isolation for gateway routes KPI, SIEM, and Agents."""
from __future__ import annotations

import json
import time

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from gateway.tenant_context import TenantContext


# ── App factories ─────────────────────────────────────────────────────────────

def _make_app(router, redis=None, ctx: TenantContext | None = None) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis
    if ctx is not None:
        @app.middleware("http")
        async def _inject(request, call_next):
            request.state.tenant = ctx
            return await call_next(request)
    app.include_router(router)
    return app


def _kpi_app(redis=None, ctx: TenantContext | None = None) -> FastAPI:
    from gateway.routes.kpi import router
    return _make_app(router, redis, ctx)


def _siem_app(redis=None, ctx: TenantContext | None = None) -> FastAPI:
    from gateway.routes.siem import router
    return _make_app(router, redis, ctx)


def _agents_app(redis=None, ctx: TenantContext | None = None) -> FastAPI:
    from gateway.routes.agents import router
    return _make_app(router, redis, ctx)


def _webhook_app(redis=None, kafka=None, ctx: TenantContext | None = None) -> FastAPI:
    from gateway.routes.agent_webhook import router
    app = _make_app(router, redis, ctx)
    app.state.kafka = kafka
    app.state.kafka_topic_evidence = "omni-diagnostic-evidence"
    return app


def _commands_app(redis=None, ctx: TenantContext | None = None) -> FastAPI:
    from gateway.routes.agent_commands import router
    return _make_app(router, redis, ctx)


# ── resolve_scope unit tests ──────────────────────────────────────────────────

class TestResolveScope:
    def test_lab_mode_returns_none(self):
        from gateway.tenant_context import resolve_scope
        assert resolve_scope(None) is None

    def test_admin_no_override_returns_none(self):
        from gateway.tenant_context import resolve_scope
        ctx = TenantContext(tenant_id="admin", is_admin=True)
        assert resolve_scope(ctx) is None

    def test_admin_with_override_returns_override(self):
        from gateway.tenant_context import resolve_scope
        ctx = TenantContext(tenant_id="admin", is_admin=True)
        assert resolve_scope(ctx, "tenantA") == "tenantA"

    def test_non_admin_ignores_override(self):
        from gateway.tenant_context import resolve_scope
        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        assert resolve_scope(ctx, "tenantB") == "tenantA"

    def test_non_admin_returns_own_tenant(self):
        from gateway.tenant_context import resolve_scope
        ctx = TenantContext(tenant_id="tenantX", is_admin=False)
        assert resolve_scope(ctx) == "tenantX"


# ── KPI isolation ─────────────────────────────────────────────────────────────

class TestKpiTenantIsolation:
    @pytest.mark.asyncio
    async def test_tenant_only_sees_own_kpi(self):
        redis = FakeRedis(decode_responses=True)
        now = time.time()
        for i in range(5):
            await redis.zadd("omni:kpi:z:tenantA:accepted", {f"a{i}": now - i})
        for i in range(10):
            await redis.zadd("omni:kpi:z:tenantB:accepted", {f"b{i}": now - i})

        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        app = _kpi_app(redis, ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/summary")

        assert resp.status_code == 200
        assert resp.json()["advisory"]["accepted"] == 5

    @pytest.mark.asyncio
    async def test_admin_aggregates_all_tenants(self):
        redis = FakeRedis(decode_responses=True)
        now = time.time()
        for i in range(5):
            await redis.zadd("omni:kpi:z:tenantA:accepted", {f"a{i}": now - i})
        for i in range(10):
            await redis.zadd("omni:kpi:z:tenantB:accepted", {f"b{i}": now - i})

        ctx = TenantContext(tenant_id="admin", is_admin=True)
        app = _kpi_app(redis, ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/summary")

        assert resp.json()["advisory"]["accepted"] == 15

    @pytest.mark.asyncio
    async def test_admin_can_scope_to_specific_tenant(self):
        redis = FakeRedis(decode_responses=True)
        now = time.time()
        for i in range(5):
            await redis.zadd("omni:kpi:z:tenantA:accepted", {f"a{i}": now - i})
        for i in range(10):
            await redis.zadd("omni:kpi:z:tenantB:accepted", {f"b{i}": now - i})

        ctx = TenantContext(tenant_id="admin", is_admin=True)
        app = _kpi_app(redis, ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/summary?tenant_id=tenantA")

        assert resp.json()["advisory"]["accepted"] == 5

    @pytest.mark.asyncio
    async def test_non_admin_cannot_scope_override(self):
        redis = FakeRedis(decode_responses=True)
        now = time.time()
        for i in range(5):
            await redis.zadd("omni:kpi:z:tenantA:accepted", {f"a{i}": now - i})
        for i in range(10):
            await redis.zadd("omni:kpi:z:tenantB:accepted", {f"b{i}": now - i})

        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        app = _kpi_app(redis, ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/summary?tenant_id=tenantB")

        assert resp.json()["advisory"]["accepted"] == 5  # still tenantA

    @pytest.mark.asyncio
    async def test_lab_mode_aggregates_all(self):
        redis = FakeRedis(decode_responses=True)
        now = time.time()
        for i in range(3):
            await redis.zadd("omni:kpi:z:tenantA:accepted", {f"a{i}": now - i})
        for i in range(4):
            await redis.zadd("omni:kpi:z:tenantB:accepted", {f"b{i}": now - i})

        app = _kpi_app(redis, ctx=None)  # lab mode
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/summary")

        assert resp.json()["advisory"]["accepted"] == 7

    @pytest.mark.asyncio
    async def test_trend_tenant_scoped(self):
        redis = FakeRedis(decode_responses=True)
        now = time.time()
        for i in range(5):
            await redis.zadd("omni:kpi:detected:tenantA:os_host", {f"d{i}": now - i * 60})
        for i in range(8):
            await redis.zadd("omni:kpi:detected:tenantB:os_host", {f"x{i}": now - i * 60})

        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        app = _kpi_app(redis, ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/trend?window=1h")

        assert resp.json()["domains"]["os_host"]["detected"] == 5

    @pytest.mark.asyncio
    async def test_trend_admin_aggregates(self):
        redis = FakeRedis(decode_responses=True)
        now = time.time()
        for i in range(5):
            await redis.zadd("omni:kpi:detected:tenantA:os_host", {f"d{i}": now - i * 60})
        for i in range(8):
            await redis.zadd("omni:kpi:detected:tenantB:os_host", {f"x{i}": now - i * 60})

        ctx = TenantContext(tenant_id="admin", is_admin=True)
        app = _kpi_app(redis, ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/trend?window=1h")

        assert resp.json()["domains"]["os_host"]["detected"] == 13


# ── SIEM isolation ────────────────────────────────────────────────────────────

class TestSiemTenantIsolation:
    def _block(self, tid: str, verdict: str = "APPROVED") -> str:
        return json.dumps({
            "seq": 1,
            "event_type": "ADVISORY_DECISION",
            "trace_id": f"tid-{tid}",
            "timestamp_utc": "2026-01-01T00:00:00Z",
            "payload": {"verdict": verdict, "root_cause": f"cause-{tid}"},
            "block_hash": "a" * 32,
        })

    @pytest.mark.asyncio
    async def test_tenant_sees_own_crat_chain(self):
        redis = FakeRedis(decode_responses=True)
        await redis.rpush("audit_chain:tenantA:blocks", self._block("tenantA"))
        await redis.rpush("audit_chain:tenantB:blocks", self._block("tenantB", "REJECTED"))
        await redis.set("audit_chain:tenantA:seq", "1")
        await redis.set("audit_chain:tenantA:head_hash", "abc")

        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        app = _siem_app(redis, ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/siem/overview")

        body = resp.json()
        assert resp.status_code == 200
        trace_ids = [b["trace_id"] for b in body["recent_blocks"]]
        assert "tid-tenantA" in trace_ids
        assert "tid-tenantB" not in trace_ids

    @pytest.mark.asyncio
    async def test_admin_default_reads_global_chain(self):
        redis = FakeRedis(decode_responses=True)
        await redis.rpush("audit_chain:blocks", self._block("global"))
        await redis.set("audit_chain:seq", "1")
        await redis.set("audit_chain:head_hash", "ggg")

        ctx = TenantContext(tenant_id="admin", is_admin=True)
        app = _siem_app(redis, ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/siem/overview")

        trace_ids = [b["trace_id"] for b in resp.json()["recent_blocks"]]
        assert "tid-global" in trace_ids

    @pytest.mark.asyncio
    async def test_admin_scopes_to_tenant(self):
        redis = FakeRedis(decode_responses=True)
        await redis.rpush("audit_chain:tenantA:blocks", self._block("tenantA"))
        await redis.set("audit_chain:tenantA:seq", "1")
        await redis.set("audit_chain:tenantA:head_hash", "aaa")

        ctx = TenantContext(tenant_id="admin", is_admin=True)
        app = _siem_app(redis, ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/siem/overview?tenant_id=tenantA")

        trace_ids = [b["trace_id"] for b in resp.json()["recent_blocks"]]
        assert "tid-tenantA" in trace_ids

    @pytest.mark.asyncio
    async def test_lab_mode_reads_global_chain(self):
        redis = FakeRedis(decode_responses=True)
        await redis.rpush("audit_chain:blocks", self._block("lab"))
        await redis.set("audit_chain:seq", "1")
        await redis.set("audit_chain:head_hash", "lll")

        app = _siem_app(redis, ctx=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/siem/overview")

        trace_ids = [b["trace_id"] for b in resp.json()["recent_blocks"]]
        assert "tid-lab" in trace_ids


# ── Agents isolation ──────────────────────────────────────────────────────────

class TestAgentsTenantIsolation:
    @pytest.mark.asyncio
    async def test_tenant_sees_only_own_worker_heartbeats(self):
        redis = FakeRedis(decode_responses=True)
        now = int(time.time())
        hb_a = json.dumps({"role": "analyst", "status": "ok", "updated_at": now})
        hb_b = json.dumps({"role": "prober", "status": "ok", "updated_at": now})
        await redis.set("omni:agent:heartbeat:tenantA:analyst", hb_a)
        await redis.set("omni:agent:heartbeat:tenantB:prober", hb_b)

        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        app = _agents_app(redis, ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents")

        roles = [w.get("role") for w in resp.json()["workers"]]
        assert "analyst" in roles
        assert "prober" not in roles

    @pytest.mark.asyncio
    async def test_admin_sees_all_worker_heartbeats(self):
        redis = FakeRedis(decode_responses=True)
        now = int(time.time())
        await redis.set("omni:agent:heartbeat:tenantA:analyst",
                        json.dumps({"role": "analyst", "status": "ok", "updated_at": now}))
        await redis.set("omni:agent:heartbeat:tenantB:prober",
                        json.dumps({"role": "prober", "status": "ok", "updated_at": now}))

        ctx = TenantContext(tenant_id="admin", is_admin=True)
        app = _agents_app(redis, ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents")

        roles = [w.get("role") for w in resp.json()["workers"]]
        assert "analyst" in roles
        assert "prober" in roles

    @pytest.mark.asyncio
    async def test_tenant_sees_only_own_remote_agents(self):
        redis = FakeRedis(decode_responses=True)
        now = int(time.time())
        await redis.set("omni:remote_agent:registry:ag-a",
                        json.dumps({"agent_id": "ag-a", "tenant_id": "tenantA", "last_seen": now - 5}))
        await redis.set("omni:remote_agent:registry:ag-b",
                        json.dumps({"agent_id": "ag-b", "tenant_id": "tenantB", "last_seen": now - 5}))

        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        app = _agents_app(redis, ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents")

        ids = [a.get("agent_id") for a in resp.json()["remote_agents"]]
        assert "ag-a" in ids
        assert "ag-b" not in ids

    @pytest.mark.asyncio
    async def test_admin_sees_all_remote_agents(self):
        redis = FakeRedis(decode_responses=True)
        now = int(time.time())
        await redis.set("omni:remote_agent:registry:ag-a",
                        json.dumps({"agent_id": "ag-a", "tenant_id": "tenantA", "last_seen": now - 5}))
        await redis.set("omni:remote_agent:registry:ag-b",
                        json.dumps({"agent_id": "ag-b", "tenant_id": "tenantB", "last_seen": now - 5}))

        ctx = TenantContext(tenant_id="admin", is_admin=True)
        app = _agents_app(redis, ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents")

        ids = [a.get("agent_id") for a in resp.json()["remote_agents"]]
        assert "ag-a" in ids
        assert "ag-b" in ids

    @pytest.mark.asyncio
    async def test_write_guard_blocks_cross_tenant_delete(self):
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:ag-b",
                        json.dumps({"agent_id": "ag-b", "tenant_id": "tenantB",
                                    "last_seen": int(time.time())}))

        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        app = _agents_app(redis, ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete("/agents/remote/ag-b")

        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_tenant_can_delete_own_agent(self):
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:ag-a",
                        json.dumps({"agent_id": "ag-a", "tenant_id": "tenantA",
                                    "last_seen": int(time.time())}))

        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        app = _agents_app(redis, ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete("/agents/remote/ag-a")

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_admin_can_delete_any_tenant_agent(self):
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:ag-b",
                        json.dumps({"agent_id": "ag-b", "tenant_id": "tenantB",
                                    "last_seen": int(time.time())}))

        ctx = TenantContext(tenant_id="admin", is_admin=True)
        app = _agents_app(redis, ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete("/agents/remote/ag-b")

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_lab_mode_sees_all_agents(self):
        redis = FakeRedis(decode_responses=True)
        now = int(time.time())
        await redis.set("omni:agent:heartbeat:tenantA:analyst",
                        json.dumps({"role": "analyst", "status": "ok", "updated_at": now}))
        await redis.set("omni:agent:heartbeat:tenantB:prober",
                        json.dumps({"role": "prober", "status": "ok", "updated_at": now}))

        app = _agents_app(redis, ctx=None)  # lab mode
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents")

        roles = [w.get("role") for w in resp.json()["workers"]]
        assert "analyst" in roles
        assert "prober" in roles


# ── is_admin_ctx ──────────────────────────────────────────────────────────────

def test_is_admin_ctx_none_returns_true():
    from gateway.tenant_context import is_admin_ctx
    assert is_admin_ctx(None) is True


def test_is_admin_ctx_non_admin():
    from gateway.tenant_context import is_admin_ctx
    ctx = TenantContext(tenant_id="customer", is_admin=False)
    assert is_admin_ctx(ctx) is False


# ── require_agent_tenant unit tests ───────────────────────────────────────────

class TestRequireAgentTenant:
    @pytest.mark.asyncio
    async def test_admin_always_allowed(self):
        from gateway.tenant_context import require_agent_tenant
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:ag-1",
                        json.dumps({"agent_id": "ag-1", "tenant_id": "tenantB"}))
        ctx = TenantContext(tenant_id="admin", is_admin=True)
        await require_agent_tenant(redis, "ag-1", ctx)  # no raise

    @pytest.mark.asyncio
    async def test_lab_mode_none_ctx_always_allowed(self):
        from gateway.tenant_context import require_agent_tenant
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:ag-1",
                        json.dumps({"agent_id": "ag-1", "tenant_id": "tenantB"}))
        await require_agent_tenant(redis, "ag-1", None)  # no raise

    @pytest.mark.asyncio
    async def test_no_existing_record_allows_first_registration(self):
        from gateway.tenant_context import require_agent_tenant
        redis = FakeRedis(decode_responses=True)
        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        await require_agent_tenant(redis, "brand-new-agent", ctx)  # no raise

    @pytest.mark.asyncio
    async def test_matching_tenant_allowed(self):
        from gateway.tenant_context import require_agent_tenant
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:ag-1",
                        json.dumps({"agent_id": "ag-1", "tenant_id": "tenantA"}))
        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        await require_agent_tenant(redis, "ag-1", ctx)  # no raise

    @pytest.mark.asyncio
    async def test_cross_tenant_rejected(self):
        from fastapi import HTTPException
        from gateway.tenant_context import require_agent_tenant
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:ag-1",
                        json.dumps({"agent_id": "ag-1", "tenant_id": "tenantB"}))
        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        with pytest.raises(HTTPException) as exc:
            await require_agent_tenant(redis, "ag-1", ctx)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_per_agent_credential_rejected_for_different_agent_id(self):
        """A credential scoped to agent-A (ctx.agent_id set) must not be usable
        to target agent-B, even with no registry record yet for agent-B and
        even under the SAME tenant — this is the P0-4 fix."""
        from fastapi import HTTPException
        from gateway.tenant_context import require_agent_tenant
        redis = FakeRedis(decode_responses=True)
        ctx = TenantContext(tenant_id="tenantA", is_admin=False, agent_id="agent-A")
        with pytest.raises(HTTPException) as exc:
            await require_agent_tenant(redis, "agent-B", ctx)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_per_agent_credential_allowed_for_own_agent_id(self):
        from gateway.tenant_context import require_agent_tenant
        redis = FakeRedis(decode_responses=True)
        ctx = TenantContext(tenant_id="tenantA", is_admin=False, agent_id="agent-A")
        await require_agent_tenant(redis, "agent-A", ctx)  # no raise

    @pytest.mark.asyncio
    async def test_tenant_shared_key_unaffected_by_agent_scoping(self):
        """ctx.agent_id=None (tenant-shared key, not a per-agent credential) keeps
        the pre-existing first-claim-wins behavior — this fix must not regress
        tenant-shared-key deployments that never adopted per-agent enrollment."""
        from gateway.tenant_context import require_agent_tenant
        redis = FakeRedis(decode_responses=True)
        ctx = TenantContext(tenant_id="tenantA", is_admin=False, agent_id=None)
        await require_agent_tenant(redis, "brand-new-agent", ctx)  # no raise


# ── agent_webhook tenant isolation ────────────────────────────────────────────

class TestAgentWebhookTenantIsolation:
    @pytest.mark.asyncio
    async def test_register_binds_non_admin_tenant_ignoring_body(self):
        redis = FakeRedis(decode_responses=True)
        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        app = _webhook_app(redis, ctx=ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/webhook/agent/register", json={
                "agent_id": "ag-1", "hostname": "h1", "tenant_id": "spoofed-tenant",
            })
        assert resp.status_code == 200
        rec = json.loads(await redis.get("omni:remote_agent:registry:ag-1"))
        assert rec["tenant_id"] == "tenantA"

    @pytest.mark.asyncio
    async def test_register_cross_tenant_agent_id_rejected(self):
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:ag-1",
                        json.dumps({"agent_id": "ag-1", "tenant_id": "tenantB"}))
        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        app = _webhook_app(redis, ctx=ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/webhook/agent/register", json={
                "agent_id": "ag-1", "hostname": "h1",
            })
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_evidence_binds_non_admin_tenant_ignoring_body(self):
        from unittest.mock import AsyncMock
        redis = FakeRedis(decode_responses=True)
        fake_kafka = AsyncMock()
        fake_kafka.send_and_wait = AsyncMock()
        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        app = _webhook_app(redis, kafka=fake_kafka, ctx=ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/webhook/agent/evidence", json={
                "agent_id": "ag-1", "hostname": "h1", "tenant_id": "spoofed-tenant",
                "evidence": [{
                    "trace_id": "t1", "probe": "p", "result": "FAILED",
                    "extracted_fact": {"x": 1}, "lane": "SYS_RESOURCE",
                }],
            })
        assert resp.status_code == 200
        payload = json.loads(json.loads(fake_kafka.send_and_wait.call_args[1]["value"])["data"])
        assert payload["tenant_id"] == "tenantA"

    @pytest.mark.asyncio
    async def test_evidence_cross_tenant_agent_id_rejected(self):
        from unittest.mock import AsyncMock
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:ag-1",
                        json.dumps({"agent_id": "ag-1", "tenant_id": "tenantB"}))
        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        app = _webhook_app(redis, kafka=AsyncMock(), ctx=ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/webhook/agent/evidence", json={
                "agent_id": "ag-1", "hostname": "h1", "evidence": [],
            })
        assert resp.status_code == 403


# ── agent_commands tenant isolation ───────────────────────────────────────────

class TestAgentCommandsTenantIsolation:
    @pytest.mark.asyncio
    async def test_poll_commands_cross_tenant_rejected(self):
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:ag-1",
                        json.dumps({"agent_id": "ag-1", "tenant_id": "tenantB"}))
        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        app = _commands_app(redis, ctx=ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/webhook/agent/commands/ag-1")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_enqueue_commands_cross_tenant_rejected(self):
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:ag-1",
                        json.dumps({"agent_id": "ag-1", "tenant_id": "tenantB"}))
        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        app = _commands_app(redis, ctx=ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/webhook/agent/commands/enqueue", json={
                "agent_id": "ag-1", "commands": [{"command": "ls"}],
            })
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_update_rejected_for_non_admin_even_when_owner(self):
        import os
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:ag-1",
                        json.dumps({"agent_id": "ag-1", "tenant_id": "tenantA"}))
        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        app = _commands_app(redis, ctx=ctx)
        os.environ["OMNI_AGENT_UPDATE_ALLOWED_HOSTS"] = "updates.example.com"
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/webhook/agent/update", json={
                    "agent_id": "ag-1", "version": "1.0.1",
                    "download_url": "https://updates.example.com/agent.tar.gz",
                    "sha256_checksum": "a" * 64,
                })
        finally:
            del os.environ["OMNI_AGENT_UPDATE_ALLOWED_HOSTS"]
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_update_allowed_for_admin(self):
        import os
        redis = FakeRedis(decode_responses=True)
        ctx = TenantContext(tenant_id="admin", is_admin=True)
        app = _commands_app(redis, ctx=ctx)
        os.environ["OMNI_AGENT_UPDATE_ALLOWED_HOSTS"] = "updates.example.com"
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/webhook/agent/update", json={
                    "agent_id": "ag-1", "version": "1.0.1",
                    "download_url": "https://updates.example.com/agent.tar.gz",
                    "sha256_checksum": "a" * 64,
                })
        finally:
            del os.environ["OMNI_AGENT_UPDATE_ALLOWED_HOSTS"]
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_list_versions_filters_by_tenant(self):
        redis = FakeRedis(decode_responses=True)
        now = int(time.time())
        await redis.set("omni:remote_agent:registry:ag-a",
                        json.dumps({"agent_id": "ag-a", "tenant_id": "tenantA", "last_seen": now}))
        await redis.set("omni:remote_agent:registry:ag-b",
                        json.dumps({"agent_id": "ag-b", "tenant_id": "tenantB", "last_seen": now}))
        ctx = TenantContext(tenant_id="tenantA", is_admin=False)
        app = _commands_app(redis, ctx=ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/webhook/agent/versions")
        ids = [a["agent_id"] for a in resp.json()["agents"]]
        assert ids == ["ag-a"]

    @pytest.mark.asyncio
    async def test_list_versions_admin_sees_all(self):
        redis = FakeRedis(decode_responses=True)
        now = int(time.time())
        await redis.set("omni:remote_agent:registry:ag-a",
                        json.dumps({"agent_id": "ag-a", "tenant_id": "tenantA", "last_seen": now}))
        await redis.set("omni:remote_agent:registry:ag-b",
                        json.dumps({"agent_id": "ag-b", "tenant_id": "tenantB", "last_seen": now}))
        ctx = TenantContext(tenant_id="admin", is_admin=True)
        app = _commands_app(redis, ctx=ctx)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/webhook/agent/versions")
        ids = sorted(a["agent_id"] for a in resp.json()["agents"])
        assert ids == ["ag-a", "ag-b"]
