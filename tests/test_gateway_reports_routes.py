"""Endpoint cho báo cáo SRE / capacity advice / playbook tốt nghiệp.

Dữ liệu do `capacity_report_loop` (G4) publish vào Redis và `playbook_graduation` (G1)
ghi vào Postgres, nhưng trước đây KHÔNG có endpoint nào đọc — portal không thể hiển thị.

Trọng tâm test: **cách ly tenant**. Tenant admin chỉ được thấy dữ liệu của chính mình,
kể cả khi cố truyền `tenant_id` của người khác trên query string.
"""

from __future__ import annotations

import contextlib
import json

import fakeredis.aioredis
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.routes.reports import router
from gateway.tenant_context import TenantContext


class _Repo:
    def __init__(self):
        self.rows = {
            "acme": [{"playbook_id": "pb-acme", "state": "GRADUATED",
                      "success_count": 3, "fail_count": 0, "domain": "advisory"}],
            "globex": [{"playbook_id": "pb-globex", "state": "GRADUATED",
                        "success_count": 9, "fail_count": 1, "domain": "advisory"}],
        }

    async def list_playbook_graduations(self, tenant_id, *, state=None):
        return list(self.rows.get(tenant_id, []))


async def _seed(redis) -> None:
    await redis.set("omni:report:sre:acme", "# Báo cáo vận hành hệ thống — acme")
    await redis.set("omni:report:sre:globex", "# BÍ MẬT CỦA GLOBEX")
    await redis.set("omni:capacity:advice:acme", json.dumps(
        [{"tenant_id": "acme", "host": "db-1", "metric": "cpu", "action": "HOLD",
          "summary": "ổn định", "auto_execute": False}]))
    await redis.set("omni:capacity:advice:globex", json.dumps(
        [{"tenant_id": "globex", "host": "secret-host", "metric": "mem",
          "action": "SCALE_UP", "summary": "bí mật", "auto_execute": False}]))


@contextlib.contextmanager
def _client(ctx: TenantContext | None, *, seed: bool = True):
    """TestClient với ctx gắn vào request.state — giống hệt gateway thật.

    Seed chạy trong lifespan để fakeredis được await trên CÙNG event loop mà
    TestClient dùng; await ở loop khác sẽ ném RuntimeError.
    """
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI):
        if seed:
            await _seed(redis)
        yield

    app = FastAPI(lifespan=_lifespan)

    @app.middleware("http")
    async def _inject_ctx(request, call_next):
        request.state.tenant = ctx
        return await call_next(request)

    app.include_router(router)
    app.state.redis = redis
    app.state.admin_repo = _Repo()

    with TestClient(app) as client:
        yield client


def test_tenant_sees_own_report():
    with _client(TenantContext(tenant_id="acme", is_admin=False)) as c:
        r = c.get("/reports/sre")

    assert r.status_code == 200
    assert "acme" in r.json()["report"]


def test_tenant_cannot_read_another_tenant_report_via_query():
    """Cố ý truyền tenant_id người khác — phải bị bỏ qua, không phải phục vụ."""
    with _client(TenantContext(tenant_id="acme", is_admin=False)) as c:
        r = c.get("/reports/sre?tenant_id=globex")

    assert r.status_code == 200
    assert "BÍ MẬT" not in r.json()["report"]
    assert "acme" in r.json()["report"]


def test_admin_may_target_a_tenant_explicitly():
    with _client(TenantContext(tenant_id="omni", is_admin=True)) as c:
        r = c.get("/reports/sre?tenant_id=globex")

    assert r.status_code == 200
    assert "GLOBEX" in r.json()["report"]


def test_missing_report_returns_404_not_empty_string():
    with _client(TenantContext(tenant_id="nobody", is_admin=False)) as c:
        r = c.get("/reports/sre")

    assert r.status_code == 404


def test_capacity_advice_is_tenant_scoped():
    with _client(TenantContext(tenant_id="acme", is_admin=False)) as c:
        r = c.get("/reports/capacity?tenant_id=globex")

    assert r.status_code == 200
    hosts = [a["host"] for a in r.json()["advice"]]
    assert "secret-host" not in hosts
    assert hosts == ["db-1"]


def test_capacity_advice_never_exposes_executable_action():
    with _client(TenantContext(tenant_id="acme", is_admin=False)) as c:
        r = c.get("/reports/capacity")

    item = r.json()["advice"][0]
    assert "tool" not in item and "args" not in item
    assert item["auto_execute"] is False


def test_graduations_are_tenant_scoped():
    with _client(TenantContext(tenant_id="acme", is_admin=False)) as c:
        r = c.get("/reports/playbooks?tenant_id=globex")

    ids = [p["playbook_id"] for p in r.json()["playbooks"]]
    assert ids == ["pb-acme"]


def test_empty_capacity_returns_empty_list_not_error():
    with _client(TenantContext(tenant_id="fresh", is_admin=False), seed=False) as c:
        r = c.get("/reports/capacity")

    assert r.status_code == 200
    assert r.json()["advice"] == []
