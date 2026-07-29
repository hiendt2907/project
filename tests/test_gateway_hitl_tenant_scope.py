"""Cách ly tenant cho hàng đợi HITL (`/autonomy/hitl/*`).

Trước bản vá, cả hai endpoint lấy ``tenant_id`` thẳng từ client — query string ở
``/pending`` và **request body** ở ``/decide`` — mà không đối chiếu danh tính. Tenant A
đọc được hàng đợi của tenant B, và **phê duyệt được mutation đang chờ của tenant B**;
quyết định đó vào ledger CRAT dưới tên A rồi publish Kafka cho worker thực thi.

SQL trong repo vốn đã lọc ``WHERE tenant_id = $2``. Lỗ hổng KHÔNG ở tầng truy vấn —
nó ở chỗ client tự quyết định giá trị đem đi lọc. Các test dưới đây tấn công đúng
điểm đó, không phải kiểm tra rằng SQL có mệnh đề WHERE.
"""

from __future__ import annotations

import contextlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.routes.autonomy import router
from gateway.tenant_context import TenantContext


class _Repo:
    """Ghi lại tenant_id THỰC SỰ tới được tầng dữ liệu — đó là thứ cần khẳng định."""

    def __init__(self):
        self.pending = {
            "acme": [{"pending_id": "p-acme", "tool_name": "k8s_scale", "risk_class": "LOW",
                      "tier_at_time": "shadow", "decision": "PENDING", "channel": "ui",
                      "actor": "", "created_at": None, "decided_at": None}],
            "globex": [{"pending_id": "p-globex", "tool_name": "k8s_delete_pod",
                        "risk_class": "HIGH", "tier_at_time": "minimal", "decision": "PENDING",
                        "channel": "ui", "actor": "", "created_at": None, "decided_at": None}],
        }
        self.decide_calls: list[tuple[str, str]] = []   # (pending_id, tenant_id)

    async def list_hitl_pending(self, tenant_id="default"):
        return list(self.pending.get(tenant_id, []))

    async def decide_hitl(self, *, pending_id, decision, actor, channel="ui", tenant_id="default"):
        self.decide_calls.append((pending_id, tenant_id))
        for row in self.pending.get(tenant_id, []):
            if row["pending_id"] == pending_id:
                return {"pending_id": pending_id, "tool_name": row["tool_name"],
                        "decision": decision}
        # Giống repo thật: không thấy trong phạm vi tenant ⇒ ValueError ⇒ HTTP 409.
        raise ValueError(f"hitl pending {pending_id!r} không tồn tại")


@contextlib.contextmanager
def _client(ctx: TenantContext | None):
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.tenant = ctx
        return await call_next(request)

    app.include_router(router)
    app.state.admin_repo = _Repo()
    app.state.kafka = None          # không publish trong test
    with TestClient(app) as client:
        yield client, app.state.admin_repo


def test_tenant_cannot_list_another_tenants_queue():
    """Cố ý truyền tenant_id người khác trên query string."""
    with _client(TenantContext(tenant_id="acme", is_admin=False)) as (c, _):
        r = c.get("/autonomy/hitl/pending?tenant_id=globex")

    assert r.status_code == 200
    ids = [p["pending_id"] for p in r.json()["pending"]]
    assert ids == ["p-acme"], "rò rỉ hàng đợi HITL của tenant khác"
    assert r.json()["tenant_id"] == "acme"


def test_tenant_cannot_decide_another_tenants_pending():
    """Lỗ hổng nghiêm trọng nhất: PHÊ DUYỆT mutation đang chờ của tenant khác.

    tenant_id nằm trong BODY nên rất dễ bị bỏ sót khi rà soát — query string còn
    nhìn thấy trên log, body thì không.
    """
    with _client(TenantContext(tenant_id="acme", is_admin=False)) as (c, repo):
        r = c.post("/autonomy/hitl/p-globex/decide",
                   json={"decision": "APPROVED", "actor": "kẻ tấn công",
                         "tenant_id": "globex"})

    # Bị ép về tenant của chính mình ⇒ không tìm thấy p-globex ⇒ 409.
    assert r.status_code == 409, "tenant A phê duyệt được pending của tenant B"
    assert repo.decide_calls == [("p-globex", "acme")], \
        f"tenant_id do client kiểm soát đã tới tầng dữ liệu: {repo.decide_calls}"


def test_tenant_can_decide_own_pending():
    """Bản vá không được chặn nhầm luồng hợp lệ."""
    with _client(TenantContext(tenant_id="acme", is_admin=False)) as (c, repo):
        r = c.post("/autonomy/hitl/p-acme/decide",
                   json={"decision": "APPROVED", "actor": "sre@acme", "tenant_id": "acme"})

    assert r.status_code == 200
    assert r.json()["decision"] == "APPROVED"
    assert repo.decide_calls == [("p-acme", "acme")]


def test_admin_may_target_a_tenant_explicitly():
    with _client(TenantContext(tenant_id="omni", is_admin=True)) as (c, _):
        r = c.get("/autonomy/hitl/pending?tenant_id=globex")

    assert r.status_code == 200
    assert [p["pending_id"] for p in r.json()["pending"]] == ["p-globex"]


def test_lab_context_none_keeps_backward_compat():
    """ctx=None là chế độ lab — giữ nguyên hành vi cũ, không phá script hiện có."""
    with _client(None) as (c, _):
        r = c.get("/autonomy/hitl/pending?tenant_id=globex")

    assert r.status_code == 200
    assert [p["pending_id"] for p in r.json()["pending"]] == ["p-globex"]
