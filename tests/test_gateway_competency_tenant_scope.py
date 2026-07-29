"""Cách ly tenant cho `/competency/*`.

Lỗ hổng vừa vá ở `/autonomy/hitl/*` (xem `test_gateway_hitl_tenant_scope.py`) là:
SQL có lọc ``WHERE tenant_id``, nhưng **client tự quyết định giá trị đem đi lọc**.
Route này có đúng hai bề mặt tương tự, và hậu quả nặng hơn một bậc:

- ``/competency/patterns`` để lộ hồ sơ năng lực — bằng chứng kinh doanh của tenant khác.
- ``/competency/scope-requests/{id}/decide`` **CẤP QUYỀN THỰC THI** cho một pattern.

Các test dưới đây tấn công đúng điểm đó, không kiểm tra rằng SQL có mệnh đề WHERE.
"""

from __future__ import annotations

import contextlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.routes.competency import router
from gateway.tenant_context import TenantContext


class _Pool:
    """Pool giả ghi lại tenant_id THỰC SỰ tới được tầng dữ liệu."""

    def __init__(self) -> None:
        self.cases = {
            "acme": [
                {"tenant_id": "acme", "pattern_key": "pod_oom", "posture": "DIAGNOSED",
                 "diagnosis_verdict": "CORRECT", "recurred": False}
            ] * 40,
            "globex": [
                {"tenant_id": "globex", "pattern_key": "db_deadlock", "posture": "DIAGNOSED",
                 "diagnosis_verdict": "CORRECT", "recurred": False}
            ] * 40,
        }
        self.requests = [
            {"id": 1, "tenant_id": "acme", "pattern_key": "pod_oom",
             "requested_scope": "HITL_REQUIRED", "evidence": "{}", "state": "PENDING",
             "cooldown_until": None, "created_at": None},
            {"id": 2, "tenant_id": "globex", "pattern_key": "db_deadlock",
             "requested_scope": "AUTO_EXECUTE", "evidence": "{}", "state": "PENDING",
             "cooldown_until": None, "created_at": None},
        ]
        self.decide_calls: list[tuple[int, str, str]] = []

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self

    @contextlib.asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, sql, *args):
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        if "DISTINCT pattern_key" in s:
            return [{"pattern_key": c["pattern_key"]} for c in self.cases.get(args[0], [])[:1]]
        if "FROM omni_admin.case_ledger" in s:
            return [c for c in self.cases.get(args[0], []) if c["pattern_key"] == args[1]]
        if "FROM omni_admin.scope_grant" in s:
            return []
        if s.startswith("UPDATE omni_admin.scope_request"):
            rid, tenant = args[0], args[1]
            self.decide_calls.append((rid, tenant, args[2]))
            for r in self.requests:
                if r["id"] == rid and r["tenant_id"] == tenant and r["state"] == "PENDING":
                    r["state"] = args[2]
                    return [dict(r)]
            return []
        if s.startswith("SELECT * FROM omni_admin.scope_request"):
            return [dict(r) for r in self.requests if r["tenant_id"] == args[0]]
        if s.startswith("INSERT INTO omni_admin.scope_grant"):
            return [{"tenant_id": args[0], "pattern_key": args[1],
                     "granted_scope": args[2], "granted_by": args[3], "frozen": False}]
        if s.startswith("INSERT INTO omni_admin.scope_request"):
            return [{"id": 99, "tenant_id": args[0], "pattern_key": args[1],
                     "requested_scope": args[2], "evidence": args[3], "state": "PENDING"}]
        raise AssertionError(f"SQL khong duoc fake ho tro: {s[:120]}")


@contextlib.contextmanager
def _client(ctx: TenantContext | None):
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.tenant = ctx
        return await call_next(request)

    app.include_router(router)
    app.state.admin_pool = _Pool()
    with TestClient(app) as client:
        yield client, app.state.admin_pool


def test_tenant_cannot_read_another_tenants_competency():
    """Cố ý truyền tenant_id người khác trên query string."""
    with _client(TenantContext(tenant_id="acme", is_admin=False)) as (c, _):
        r = c.get("/competency/patterns?tenant_id=globex")

    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "acme"
    keys = [p["pattern_key"] for p in body["patterns"]]
    assert keys == ["pod_oom"], f"rò rỉ hồ sơ năng lực tenant khác: {keys}"


def test_tenant_cannot_read_another_tenants_scope_requests():
    with _client(TenantContext(tenant_id="acme", is_admin=False)) as (c, _):
        r = c.get("/competency/scope-requests?tenant_id=globex")

    assert r.status_code == 200
    assert [q["id"] for q in r.json()["requests"]] == [1]


def test_tenant_cannot_decide_another_tenants_scope_request():
    """Nặng nhất: duyệt đơn xin quyền của tenant khác = cấp quyền thực thi cho họ."""
    with _client(TenantContext(tenant_id="acme", is_admin=False)) as (c, pool):
        r = c.post("/competency/scope-requests/2/decide",
                   json={"decision": "APPROVED", "actor": "kẻ tấn công"})

    assert r.status_code == 404, "tenant A duyệt được đơn của tenant B"
    assert pool.decide_calls == [(2, "acme", "APPROVED")], \
        f"tenant do client kiểm soát đã tới tầng dữ liệu: {pool.decide_calls}"
    assert pool.requests[1]["state"] == "PENDING"


def test_tenant_can_decide_own_request():
    """Bản vá không được chặn nhầm luồng hợp lệ."""
    with _client(TenantContext(tenant_id="acme", is_admin=False)) as (c, pool):
        r = c.post("/competency/scope-requests/1/decide",
                   json={"decision": "APPROVED", "actor": "cto@acme"})

    assert r.status_code == 200
    assert r.json()["state"] == "APPROVED"
    assert pool.requests[0]["state"] == "APPROVED"


def test_admin_may_target_a_tenant_explicitly():
    with _client(TenantContext(tenant_id="omni", is_admin=True)) as (c, _):
        r = c.get("/competency/scope-requests?tenant_id=globex")

    assert r.status_code == 200
    assert [q["id"] for q in r.json()["requests"]] == [2]


def test_lab_context_none_keeps_backward_compat():
    with _client(None) as (c, _):
        r = c.get("/competency/scope-requests?tenant_id=globex")

    assert r.status_code == 200
    assert [q["id"] for q in r.json()["requests"]] == [2]


def test_missing_admin_pool_returns_503_not_500():
    app = FastAPI()
    app.include_router(router)
    app.state.admin_pool = None
    with TestClient(app) as c:
        assert c.get("/competency/patterns").status_code == 503


def test_invalid_decision_rejected():
    with _client(TenantContext(tenant_id="acme", is_admin=False)) as (c, _):
        r = c.post("/competency/scope-requests/1/decide",
                   json={"decision": "MAYBE", "actor": "x"})
    assert r.status_code == 400
