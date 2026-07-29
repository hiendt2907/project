"""Cách ly tenant cho CRAT export và purge dữ liệu trace.

Hai lỗ hổng audit 2026-07-29:
1. `compliance.crat_export`/`crat_stats` nhận `tenant_id` thẳng từ query rồi dựng key
   `omni:crat:blocks:{tenant_id}` mà KHÔNG gọi `resolve_scope` — dù `siem.py` (cùng đọc
   key CRAT) đã làm đúng. Đây là dữ liệu SOX §404 / PCI-DSS v4.0, export chéo tenant là
   vi phạm compliance chứ không chỉ là bug.
2. `POST /trace/purge` xoá `omni:trace:*` TOÀN HỆ THỐNG, không đòi admin — một tenant
   có thể xoá sạch dữ liệu chẩn đoán của mọi tenant khác.
"""

from __future__ import annotations

import contextlib

import fakeredis.aioredis
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.tenant_context import TenantContext

TENANT = TenantContext(tenant_id="acme", is_admin=False)
ADMIN = TenantContext(tenant_id="admin", is_admin=True)


@contextlib.contextmanager
def _client(router, ctx, seeder=None):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def _lifespan(app):
        if seeder:
            await seeder(redis)
        yield

    app = FastAPI(lifespan=_lifespan)

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.tenant = ctx
        return await call_next(request)

    app.include_router(router)
    app.state.redis = redis
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, redis


# --- CRAT export ------------------------------------------------------------

async def _seed_crat(redis):
    import json
    for tid, payload in (("acme", "acme-block"), ("globex", "GLOBEX-SECRET-BLOCK")):
        await redis.rpush(
            f"audit_chain:{tid}:blocks",
            json.dumps({"seq": 1, "event_type": "ADVISORY_DECISION",
                        "trace_id": payload, "hash": "h", "prev_hash": "p"}),
        )


def test_tenant_cannot_export_another_tenant_crat_chain():
    from gateway.routes.compliance import router

    with _client(router, TENANT, _seed_crat) as (c, _):
        r = c.get("/crat/export?tenant_id=globex&format=json")

    assert r.status_code == 200
    assert "GLOBEX-SECRET-BLOCK" not in r.text


def test_tenant_exports_own_crat_chain():
    from gateway.routes.compliance import router

    with _client(router, TENANT, _seed_crat) as (c, _):
        r = c.get("/crat/export?tenant_id=acme&format=json")

    assert r.status_code == 200
    assert "acme-block" in r.text


def test_admin_may_export_a_named_tenant():
    from gateway.routes.compliance import router

    with _client(router, ADMIN, _seed_crat) as (c, _):
        r = c.get("/crat/export?tenant_id=globex&format=json")

    assert r.status_code == 200
    assert "GLOBEX-SECRET-BLOCK" in r.text


def test_tenant_cannot_read_another_tenant_crat_stats():
    from gateway.routes.compliance import router

    with _client(router, TENANT, _seed_crat) as (c, _):
        r = c.get("/crat/stats?tenant_id=globex")

    assert r.status_code == 200
    assert r.json().get("tenant_id") == "acme"


# --- trace purge ------------------------------------------------------------

async def _seed_traces(redis):
    await redis.set("omni:trace:advisory:t-acme", "{}")
    await redis.set("omni:trace:advisory:t-globex", "{}")


def test_non_admin_cannot_purge_global_trace_state():
    from gateway.routes.trace import router

    with _client(router, TENANT, _seed_traces) as (c, redis):
        r = c.post("/trace/purge")

    assert r.status_code == 403


def test_admin_can_still_purge():
    from gateway.routes.trace import router

    with _client(router, ADMIN, _seed_traces) as (c, _):
        r = c.post("/trace/purge")

    assert r.status_code == 200
    assert r.json()["purged"] is True
