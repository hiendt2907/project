"""Endpoint reports/HITL của tenant portal (`src/aoip/console/app.py`).

Tenant portal KHÔNG đi qua gateway — nó gọi thẳng app này. Dữ liệu G4 (báo cáo SRE,
đề xuất capacity) và hàng đợi HITL vì thế phải được phơi ở đây, nếu không portal
không có gì để hiển thị.

Khác biệt quan trọng so với bề mặt gateway: ở đây tenant lấy TỪ PRINCIPAL, không có
tham số nào để client can thiệp. Test bên dưới khẳng định đúng tính chất đó — thay
đổi nào khiến các endpoint này nhận `tenant_id` từ client đều phải làm test vỡ.
"""

from __future__ import annotations

import contextlib
import json
import time
from datetime import datetime, timezone

import fakeredis.aioredis as aioredis
import httpx

from aoip.console import identity
from aoip.console.app import create_tenant_app


def _redis():
    return aioredis.FakeRedis(decode_responses=True)


async def _sid(r, *, subject="sre@acme", tenant="acme"):
    await identity.upsert_user(r, subject=subject, email=subject)
    await identity.add_membership(r, subject=subject, tenant=tenant, role="sre_lead")
    p = await identity.resolve_tenant_principal(r, subject, tenant)
    return (await identity.issue_session(r, principal=p, now=time.time())).sid


def _auth(sid):
    return {"Authorization": f"Bearer {sid}"}


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c")


async def _seed_reports(r):
    await r.set("omni:report:sre:acme", "# Báo cáo vận hành — acme")
    await r.set("omni:report:sre:globex", "# BÍ MẬT CỦA GLOBEX")
    await r.set("omni:capacity:advice:acme", json.dumps(
        [{"host": "db-1", "metric": "cpu", "action": "HOLD",
          "summary": "ổn định", "auto_execute": False}]))
    await r.set("omni:capacity:advice:globex", json.dumps(
        [{"host": "secret-host", "action": "SCALE_UP", "auto_execute": False}]))


async def test_sre_report_scoped_to_principal_tenant():
    r = _redis(); await _seed_reports(r); sid = await _sid(r)
    async with _client(create_tenant_app(r)) as c:
        resp = await c.get("/api/tenant/v1/reports/sre", headers=_auth(sid))

    assert resp.status_code == 200
    assert resp.json()["tenant"] == "acme"
    assert "acme" in resp.json()["report"]
    assert "BÍ MẬT" not in resp.json()["report"]


async def test_client_cannot_inject_tenant_via_query():
    """Endpoint không nhận tenant từ client dưới BẤT KỲ hình thức nào.

    Khác gateway `/reports/*` vốn buộc phải dùng resolve_scope vì nhận tenant_id
    trên query string. Ở đây tham số lạ phải bị bỏ qua hoàn toàn.
    """
    r = _redis(); await _seed_reports(r); sid = await _sid(r)
    async with _client(create_tenant_app(r)) as c:
        resp = await c.get("/api/tenant/v1/reports/sre?tenant_id=globex&tenant=globex",
                           headers=_auth(sid))

    assert resp.status_code == 200
    assert resp.json()["tenant"] == "acme"
    assert "BÍ MẬT" not in resp.json()["report"]


async def test_missing_report_returns_404_not_empty_string():
    r = _redis(); sid = await _sid(r)
    async with _client(create_tenant_app(r)) as c:
        resp = await c.get("/api/tenant/v1/reports/sre", headers=_auth(sid))

    assert resp.status_code == 404


async def test_capacity_advice_is_scoped_and_never_executable():
    r = _redis(); await _seed_reports(r); sid = await _sid(r)
    async with _client(create_tenant_app(r)) as c:
        resp = await c.get("/api/tenant/v1/reports/capacity", headers=_auth(sid))

    advice = resp.json()["advice"]
    assert [a["host"] for a in advice] == ["db-1"]
    assert "tool" not in advice[0] and "args" not in advice[0]
    assert advice[0]["auto_execute"] is False


async def test_capacity_returns_empty_list_when_absent():
    r = _redis(); sid = await _sid(r)
    async with _client(create_tenant_app(r)) as c:
        resp = await c.get("/api/tenant/v1/reports/capacity", headers=_auth(sid))

    assert resp.status_code == 200
    assert resp.json()["advice"] == []


async def test_unauthenticated_is_rejected_on_every_new_endpoint():
    r = _redis()
    async with _client(create_tenant_app(r)) as c:
        for path in ("/api/tenant/v1/reports/sre",
                     "/api/tenant/v1/reports/capacity",
                     "/api/tenant/v1/hitl/pending"):
            assert (await c.get(path)).status_code == 401, path


async def test_hitl_without_pg_returns_503_not_500():
    """Thiếu PG là tình trạng vận hành, không phải lỗi lập trình — phải nói rõ."""
    r = _redis(); sid = await _sid(r)
    app = create_tenant_app(r)
    app.state.pool = None
    async with _client(app) as c:
        resp = await c.get("/api/tenant/v1/hitl/pending", headers=_auth(sid))

    assert resp.status_code == 503


async def test_hitl_scopes_tenant_and_serializes_datetime():
    """Row Postgres có cột timestamp — JSONResponse không tự encode datetime.

    Chính kiểu lỗi này đã làm `/reports/playbooks` của gateway trả 500 trên cluster
    mà test unit không bắt được, vì mock khi đó chỉ trả kiểu nguyên thuỷ.
    """
    ts = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
    seen = {}

    class _Conn:
        async def fetch(self, _sql, tenant_id):
            seen["tenant"] = tenant_id
            return [{"pending_id": "p-1", "tool_name": "k8s_scale", "risk_class": "LOW",
                     "tier_at_time": "shadow", "channel": "ui", "actor": "",
                     "created_at": ts}]

    class _Pool:
        def acquire(self):
            @contextlib.asynccontextmanager
            async def _cm():
                yield _Conn()
            return _cm()

    r = _redis(); sid = await _sid(r)
    app = create_tenant_app(r)
    app.state.pool = _Pool()
    async with _client(app) as c:
        resp = await c.get("/api/tenant/v1/hitl/pending", headers=_auth(sid))

    assert resp.status_code == 200
    assert seen["tenant"] == "acme", "tenant không lấy từ principal"
    assert resp.json()["pending"][0]["created_at"].startswith("2026-07-29T10:00")
