"""Ba route sổ ca trên BFF tenant portal (`src/aoip/console/app.py`).

Tenant portal KHÔNG đi qua gateway — nó gọi thẳng app này, nên `/competency/*` của
gateway hoàn toàn không với tới được từ portal khách. Ba route ở đây là bản song
song, và chính vì song song mà chúng phải được bảo vệ riêng: một lỗ hổng phân quyền
đã từng sống ở đúng kiểu bề mặt này (`/hitl/{id}/decide` nhận `tenant_id` từ body,
cho phép một tenant phê duyệt mutation đang chờ của tenant khác).

Tính chất bắt buộc, thay đổi nào làm vỡ các test này đều là hồi quy bảo mật:
- tenant LUÔN lấy từ principal; client không có tham số nào can thiệp được
- duyệt đơn xin quyền là ĐỔI CHÍNH SÁCH → cần `P_CHANGE_POLICY`, không phải quyền xem
- không duyệt được đơn của tenant khác, và không lộ ra là đơn đó có tồn tại hay không
"""

from __future__ import annotations

import contextlib
import time
from datetime import datetime, timezone

import fakeredis.aioredis as aioredis
import httpx

from aoip.console import identity
from aoip.console.app import create_tenant_app


def _redis():
    return aioredis.FakeRedis(decode_responses=True)


async def _sid(r, *, subject="sre@acme", tenant="acme", role="sre_lead"):
    await identity.upsert_user(r, subject=subject, email=subject)
    await identity.add_membership(r, subject=subject, tenant=tenant, role=role)
    p = await identity.resolve_tenant_principal(r, subject, tenant)
    return (await identity.issue_session(r, principal=p, now=time.time())).sid


def _auth(sid):
    return {"Authorization": f"Bearer {sid}"}


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c")


class _Conn:
    """Fake asyncpg conn — ghi lại tenant thực sự tới tầng dữ liệu."""

    def __init__(self, seen: dict, rows: list[dict] | None = None) -> None:
        self._seen = seen
        self._rows = rows if rows is not None else []

    async def fetch(self, sql: str, *args):
        s = " ".join(sql.split())
        if "DISTINCT pattern_key" in s:
            self._seen.setdefault("pattern_tenants", []).append(args[0])
            return [{"pattern_key": "pk-1"}]
        if "FROM omni_admin.case_ledger" in s:
            self._seen.setdefault("case_tenants", []).append(args[0])
            return [
                {"posture": "DIAGNOSED", "diagnosis_verdict": "CORRECT", "recurred": False}
                for _ in range(30)
            ]
        if "FROM omni_admin.scope_grant" in s:
            self._seen.setdefault("grant_tenants", []).append(args[0])
            return []
        if "FROM omni_admin.scope_request" in s:
            self._seen.setdefault("request_tenants", []).append(args[0])
            return self._rows
        return []

    async def fetchrow(self, sql: str, *args):
        s = " ".join(sql.split())
        if "UPDATE omni_admin.scope_request" in s:
            # tenant_id nằm trong WHERE — đây là chỗ cách ly thật sự có hiệu lực.
            self._seen["decide_args"] = args
            if self._seen.get("decide_returns_none"):
                return None
            return {
                "id": args[0] if isinstance(args[0], int) else 1,
                "tenant_id": "acme",
                "pattern_key": "pk-1",
                "state": "APPROVED",
                "decided_at": datetime(2026, 7, 30, tzinfo=timezone.utc),
            }
        if "FROM omni_admin.scope_request" in s:
            return None
        return None

    async def execute(self, sql: str, *args):
        return "OK"

    def transaction(self):
        @contextlib.asynccontextmanager
        async def _cm():
            yield None
        return _cm()


class _Pool:
    def __init__(self, seen: dict, rows: list[dict] | None = None) -> None:
        self._seen = seen
        self._rows = rows

    def acquire(self):
        @contextlib.asynccontextmanager
        async def _cm():
            yield _Conn(self._seen, self._rows)
        return _cm()


async def test_patterns_scoped_to_principal_tenant():
    r = _redis(); sid = await _sid(r); seen: dict = {}
    app = create_tenant_app(r); app.state.pool = _Pool(seen)
    async with _client(app) as c:
        resp = await c.get("/api/tenant/v1/competency/patterns", headers=_auth(sid))

    assert resp.status_code == 200
    assert resp.json()["tenant_id"] == "acme"
    assert seen["pattern_tenants"] == ["acme"]
    assert seen["case_tenants"] == ["acme"]


async def test_client_cannot_inject_tenant_on_any_competency_route():
    """Không route nào nhận tenant từ client — tham số lạ phải bị bỏ qua hoàn toàn."""
    r = _redis(); sid = await _sid(r); seen: dict = {}
    app = create_tenant_app(r); app.state.pool = _Pool(seen)
    async with _client(app) as c:
        p = await c.get(
            "/api/tenant/v1/competency/patterns?tenant_id=globex&tenant=globex",
            headers=_auth(sid),
        )
        s = await c.get(
            "/api/tenant/v1/competency/scope-requests?tenant_id=globex",
            headers=_auth(sid),
        )

    assert p.json()["tenant_id"] == "acme"
    assert s.json()["tenant_id"] == "acme"
    assert "globex" not in seen.get("pattern_tenants", [])
    assert "globex" not in seen.get("request_tenants", [])


async def test_blockers_are_returned_not_filtered_out():
    """Pattern chưa đủ điều kiện VẪN phải hiện, kèm lý do.

    Với admin khách, `blockers` là phần đáng giá nhất — giấu pattern trượt đi thì
    trang chỉ còn là bảng thành tích.
    """
    r = _redis(); sid = await _sid(r); seen: dict = {}
    app = create_tenant_app(r); app.state.pool = _Pool(seen)
    async with _client(app) as c:
        resp = await c.get("/api/tenant/v1/competency/patterns", headers=_auth(sid))

    pat = resp.json()["patterns"][0]
    assert "accuracy_lower_bound" in pat and "coverage" in pat
    assert "blockers" in pat
    assert pat["granted_scope"] == "SUGGEST_ONLY"  # mặc định khi chưa có grant


async def test_viewer_cannot_decide_scope_request():
    """Trao quyền thực thi cho một loại việc là ĐỔI CHÍNH SÁCH, không phải xem."""
    r = _redis(); seen: dict = {}
    sid = await _sid(r, subject="viewer@acme", role="viewer")
    app = create_tenant_app(r); app.state.pool = _Pool(seen)
    async with _client(app) as c:
        resp = await c.post(
            "/api/tenant/v1/competency/scope-requests/1/decide",
            json={"decision": "APPROVED"}, headers=_auth(sid),
        )

    assert resp.status_code == 403
    assert "decide_args" not in seen, "đã chạm tầng dữ liệu dù không đủ quyền"


async def test_decide_passes_principal_tenant_into_where_clause():
    """Tenant đi vào WHERE phải là của principal — không phải giá trị client gửi."""
    r = _redis(); seen: dict = {}
    sid = await _sid(r, subject="lead@acme", role="tenant_owner")
    app = create_tenant_app(r); app.state.pool = _Pool(seen)
    async with _client(app) as c:
        resp = await c.post(
            "/api/tenant/v1/competency/scope-requests/1/decide",
            json={"decision": "APPROVED", "tenant_id": "globex", "actor": "kẻ giả mạo"},
            headers=_auth(sid),
        )

    # KHÔNG có đường thoát "nếu 403 thì bỏ qua": một test tự cho phép mình không
    # kiểm tra gì là test rỗng ruột, và nó sẽ vẫn xanh kể cả khi cách ly tenant hỏng.
    assert resp.status_code == 200, f"tenant_owner phải có P_CHANGE_POLICY (got {resp.status_code})"
    assert "acme" in seen["decide_args"], f"tenant sai: {seen['decide_args']}"
    assert "globex" not in seen["decide_args"]
    assert "kẻ giả mạo" not in seen["decide_args"], "actor lấy từ body thay vì session"


async def test_decide_rejects_unknown_decision_value():
    r = _redis(); seen: dict = {}
    sid = await _sid(r, subject="lead@acme", role="tenant_owner")
    app = create_tenant_app(r); app.state.pool = _Pool(seen)
    async with _client(app) as c:
        resp = await c.post(
            "/api/tenant/v1/competency/scope-requests/1/decide",
            json={"decision": "MAYBE"}, headers=_auth(sid),
        )

    assert resp.status_code in (400, 403)
    assert "decide_args" not in seen


async def test_missing_pool_returns_503_not_500():
    """Thiếu PG là tình trạng vận hành, không phải lỗi lập trình."""
    r = _redis(); sid = await _sid(r)
    app = create_tenant_app(r); app.state.pool = None
    async with _client(app) as c:
        for path in ("/api/tenant/v1/competency/patterns",
                     "/api/tenant/v1/competency/scope-requests"):
            assert (await c.get(path, headers=_auth(sid))).status_code == 503, path


async def test_unauthenticated_rejected_on_every_route():
    r = _redis()
    app = create_tenant_app(r); app.state.pool = _Pool({})
    async with _client(app) as c:
        assert (await c.get("/api/tenant/v1/competency/patterns")).status_code == 401
        assert (await c.get("/api/tenant/v1/competency/scope-requests")).status_code == 401
        assert (await c.post(
            "/api/tenant/v1/competency/scope-requests/1/decide",
            json={"decision": "APPROVED"},
        )).status_code == 401


async def test_datetime_is_serialized_not_500():
    """Row Postgres có TIMESTAMPTZ — JSONResponse không tự encode datetime.

    Chính kiểu lỗi này đã làm `/reports/playbooks` của gateway trả 500 trên cluster
    trong khi test unit vẫn xanh, vì mock khi đó chỉ trả kiểu nguyên thuỷ.
    """
    r = _redis(); sid = await _sid(r)
    rows = [{
        "id": 1, "tenant_id": "acme", "pattern_key": "pk-1",
        "requested_scope": "HITL_REQUIRED", "state": "PENDING",
        "evidence": {"coverage": 0.83},
        "created_at": datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc),
        "cooldown_until": None,
    }]
    app = create_tenant_app(r); app.state.pool = _Pool({}, rows)
    async with _client(app) as c:
        resp = await c.get("/api/tenant/v1/competency/scope-requests", headers=_auth(sid))

    assert resp.status_code == 200
    assert resp.json()["requests"][0]["created_at"].startswith("2026-07-30T10:00")
