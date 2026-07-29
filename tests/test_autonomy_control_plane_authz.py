"""Control-plane `/autonomy` phải phân quyền, không chỉ xác thực.

Bối cảnh (khai thác THẬT trên cluster lab 2026-07-29): `_require_api_key` chỉ XÁC THỰC
và gắn `TenantContext`; mỗi endpoint phải tự phân quyền. 21/27 endpoint `/autonomy`
không làm điều đó. Bằng chứng khai thác: key tenant `staging-sim` (is_admin=False)
gọi `POST /autonomy/tenants/default/api-keys` và **tạo thành công** một API key cho
tenant `default`, đồng thời đọc được danh sách tenant + kill-switch của tenant khác.

Dấu hiệu rõ nhất cho thấy đây là sơ suất chứ không phải thiết kế: `DELETE` api-key CÓ
`_require_admin_ctx`, còn `GET`/`POST` cùng tài nguyên thì KHÔNG — tức không được phép
thu hồi nhưng lại được phép phát hành.
"""

from __future__ import annotations

import contextlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.routes.autonomy import router
from gateway.tenant_context import TenantContext


class _Repo:
    """Repo tối thiểu — nếu authz thủng, các hàm này sẽ bị gọi và test bắt được."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def list_api_keys(self, tenant_id):
        self.calls.append(("list_api_keys", tenant_id))
        return []

    async def create_api_key(self, *, tenant_id, **kw):
        self.calls.append(("create_api_key", tenant_id))
        return {"id": 1, "key_prefix": "xxxxxxxx"}

    async def list_tenants(self):
        self.calls.append(("list_tenants", "*"))
        return []

    async def create_tenant(self, *, tenant_id, **kw):
        self.calls.append(("create_tenant", tenant_id))
        return {"tenant_id": tenant_id}

    async def set_tier(self, *, tenant_id, **kw):
        self.calls.append(("set_tier", tenant_id))
        return {"tenant_id": tenant_id}

    async def get_tier(self, tenant_id="default"):
        return "shadow"


@contextlib.contextmanager
def _client(ctx: TenantContext):
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.tenant = ctx
        return await call_next(request)

    app.include_router(router)
    repo = _Repo()
    app.state.admin_repo = repo
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, repo


TENANT = TenantContext(tenant_id="staging-sim", is_admin=False)
ADMIN = TenantContext(tenant_id="admin", is_admin=True)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/autonomy/tenants/default/api-keys", {"actor": "x", "label": "y"}),
        ("post", "/autonomy/tenants", {"tenant_id": "victim", "display_name": "V",
                                       "actor": "x"}),
        ("post", "/autonomy/tier", {"tenant_id": "default", "tier": "autonomous",
                                    "actor": "x"}),
        ("post", "/autonomy/mutation", {"tenant_id": "default", "enabled": True,
                                        "actor": "x"}),
    ],
)
def test_non_admin_cannot_mutate_control_plane(method, path, body):
    """Không tenant thường nào được đổi cấu hình tự trị của tenant khác."""
    with _client(TENANT) as (c, repo):
        r = getattr(c, method)(path, json=body)

    assert r.status_code == 403, f"{path} trả {r.status_code}, đáng lẽ 403"
    assert repo.calls == [], f"authz thủng — repo đã bị gọi: {repo.calls}"


def test_non_admin_cannot_list_another_tenant_api_keys():
    """Prefix/label/actor của key tenant khác là thông tin nội bộ, không được lộ."""
    with _client(TENANT) as (c, repo):
        r = c.get("/autonomy/tenants/default/api-keys")

    assert r.status_code == 403
    assert repo.calls == []


def test_admin_can_still_manage_api_keys():
    """Vá authz không được làm hỏng đường admin hợp lệ."""
    with _client(ADMIN) as (c, repo):
        r = c.get("/autonomy/tenants/default/api-keys")

    assert r.status_code == 200
    assert ("list_api_keys", "default") in repo.calls


def test_admin_can_still_create_tenant():
    with _client(ADMIN) as (c, repo):
        r = c.post("/autonomy/tenants",
                   json={"tenant_id": "newco", "display_name": "New", "actor": "x"})

    assert r.status_code == 200
    assert ("create_tenant", "newco") in repo.calls


def test_non_admin_tenant_list_is_scoped_not_global():
    """Tenant thường không được liệt kê toàn bộ khách hàng của platform."""
    with _client(TENANT) as (c, repo):
        r = c.get("/autonomy/tenants")

    assert r.status_code == 403
    assert repo.calls == []
