"""HITL approve/reject phải đi vào sổ ca — CẢ HAI bề mặt duyệt.

Vì sao có file này: phán quyết HITL là tín hiệu học chất lượng cao nhất trong hệ
thống (một con người cân nhắc rồi phán quyết về một hành động cụ thể sắp chạy).
Trước bản vá nó chỉ vào CRAT rồi bị vứt hoàn toàn.

Hai điều bắt buộc phải chứng minh, không phải chỉ "có gọi hàm":
1. reject qua HTTP ⇒ ``diagnosis_verdict='INCORRECT'`` NẰM TRONG sổ ca.
2. PG hỏng ⇒ quyết định HITL vẫn đi trọn vẹn. Sổ ca là bằng chứng năng lực, không
   phải đường quyết định; chỉ CRAT mới được fail-closed.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

import fakeredis.aioredis
import httpx
import pytest
from fastapi import FastAPI

from gateway.routes.autonomy import router
from services.case_ledger.hitl_link import case_id_for_hitl, record_hitl_verdict

# ── Fake asyncpg pool: chỉ hiểu đúng các câu lệnh CaseLedgerStore phát ra ──────


class _FakeConn:
    def __init__(self, rows: dict[str, dict]) -> None:
        self.rows = rows

    @contextlib.asynccontextmanager
    async def _tx(self):
        yield None

    def transaction(self):
        return self._tx()

    async def execute(self, sql: str, *args):
        # open_case lấy pg_advisory_xact_lock trước khi đọc ca gần nhất (chống hai ca
        # cùng pattern nhận cùng occurrence_no khi alert dồn dập). Fake chỉ cần nuốt.
        return "OK"

    async def fetchrow(self, sql: str, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT case_id, occurrence_no"):
            tenant, pattern = args
            cands = [
                r for r in self.rows.values()
                if r["tenant_id"] == tenant and r["pattern_key"] == pattern
            ]
            return cands[-1] if cands else None
        if s.startswith("INSERT INTO omni_admin.case_ledger"):
            (case_id, tenant_id, pattern_key, lane, alertname, posture,
             occurrence_no, prior_case_id, crat_ref, domain) = args
            if case_id in self.rows:  # ON CONFLICT DO NOTHING
                return None
            self.rows[case_id] = {
                "case_id": case_id, "tenant_id": tenant_id, "pattern_key": pattern_key,
                "lane": lane, "alertname": alertname, "posture": posture,
                "occurrence_no": occurrence_no, "prior_case_id": prior_case_id,
                "crat_ref": crat_ref, "domain": domain, "diagnosis_verdict": "UNJUDGED",
                "remedy_verdict": "UNJUDGED", "diagnosis_source": None, "diagnosis_actor": None,
                "remedy_source": None, "remedy_actor": None,
            }
            return dict(self.rows[case_id])
        if s.startswith("SELECT * FROM omni_admin.case_ledger WHERE case_id"):
            row = self.rows.get(args[0])
            return dict(row) if row else None
        if s.startswith("UPDATE omni_admin.case_ledger SET diagnosis_verdict"):
            case_id, diagnosis, remedy, source, actor, crat_ref = args
            row = self.rows.get(case_id)
            if row is None:
                return None
            if diagnosis is not None:
                row["diagnosis_verdict"] = diagnosis
            if remedy is not None:
                row["remedy_verdict"] = remedy
            if diagnosis is not None:
                row["diagnosis_source"] = source
                row["diagnosis_actor"] = actor
            if remedy is not None:
                row["remedy_source"] = source
                row["remedy_actor"] = actor
            if crat_ref is not None:
                row["crat_ref"] = crat_ref
            return dict(row)
        raise AssertionError(f"SQL ngoai du kien: {s[:80]}")


class _FakePool:
    """``.acquire()`` là asynccontextmanager, giống asyncpg."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield _FakeConn(self.rows)


class _BrokenPool:
    """PG chết. Mọi acquire nổ — đúng kiểu outage thật, không phải mock trả None."""

    @contextlib.asynccontextmanager
    async def acquire(self):
        raise RuntimeError("connection refused")
        yield  # pragma: no cover


# ── Bề mặt HTTP: POST /autonomy/hitl/{id}/decide ──────────────────────────────


class _Repo:
    def __init__(self) -> None:
        self.decide_calls: list[tuple[str, str, str]] = []

    async def decide_hitl(self, *, pending_id, decision, actor, channel="ui", tenant_id="default"):
        self.decide_calls.append((pending_id, decision, tenant_id))
        return {"pending_id": pending_id, "decision": decision, "tool_name": "k8s_delete_pod"}


def _app(pool) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.tenant = None  # lab (ctx=None) — giữ hành vi backward-compat
        return await call_next(request)

    app.include_router(router)
    app.state.admin_repo = _Repo()
    app.state.admin_pool = pool
    app.state.kafka = None
    return app


async def _post_decide(app: FastAPI, pending_id: str, decision: str, tenant: str = "acme"):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.post(
            f"/autonomy/hitl/{pending_id}/decide",
            json={"decision": decision, "actor": "sre@acme", "tenant_id": tenant},
        )


async def test_http_reject_lands_as_incorrect_in_case_ledger():
    """Yêu cầu cốt lõi: từ chối qua HTTP ⇒ INCORRECT trong sổ ca, không phải log."""
    pool = _FakePool()
    resp = await _post_decide(_app(pool), "p-1", "REJECTED")

    assert resp.status_code == 200
    case_id = case_id_for_hitl(pending_id="p-1")
    assert case_id in pool.rows, "quyết định HITL không mở được ca nào"
    row = pool.rows[case_id]
    assert row["diagnosis_verdict"] == "INCORRECT"
    assert row["diagnosis_source"] == "hitl"
    assert row["diagnosis_actor"] == "sre@acme"
    # Ca do HITL mở là ca Omni ĐÃ phát biểu — không phải ca nó từ chối.
    assert row["posture"] == "DIAGNOSED"


async def test_http_approve_marks_diagnosis_correct_but_leaves_remedy_unjudged():
    """Duyệt = người đồng ý CHẨN ĐOÁN. Hành động còn chưa chạy nên chưa ai biết nó
    có khắc phục được không — remedy phải do thế giới chấm, không phải Omni."""
    pool = _FakePool()
    resp = await _post_decide(_app(pool), "p-2", "APPROVED")

    assert resp.status_code == 200
    row = pool.rows[case_id_for_hitl(pending_id="p-2")]
    assert row["diagnosis_verdict"] == "CORRECT"
    assert row["remedy_verdict"] == "UNJUDGED"


async def test_http_decision_survives_postgres_outage():
    """PG chết KHÔNG được làm hỏng quyết định HITL — ghi sổ là best-effort."""
    app = _app(_BrokenPool())
    resp = await _post_decide(app, "p-3", "REJECTED")

    assert resp.status_code == 200, "sổ ca hỏng đã kéo sập đường quyết định HITL"
    assert resp.json()["decision"] == "REJECTED"
    assert app.state.admin_repo.decide_calls == [("p-3", "REJECTED", "acme")]


async def test_http_decision_works_without_pool_configured():
    """Lab không cấu hình OMNI_ADMIN_PG_DSN ⇒ admin_pool=None, vẫn phải chạy."""
    app = _app(None)
    resp = await _post_decide(app, "p-4", "APPROVED")
    assert resp.status_code == 200


async def test_http_ledger_uses_scoped_tenant_not_body_tenant():
    """Sổ ca không được mở lại đường ghi chéo tenant mà bản vá cách ly vừa bịt."""
    from gateway.tenant_context import TenantContext

    pool = _FakePool()
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.tenant = TenantContext(tenant_id="acme", is_admin=False)
        return await call_next(request)

    app.include_router(router)
    app.state.admin_repo = _Repo()
    app.state.admin_pool = pool
    app.state.kafka = None

    resp = await _post_decide(app, "p-5", "REJECTED", tenant="globex")
    assert resp.status_code == 200
    row = pool.rows[case_id_for_hitl(pending_id="p-5")]
    assert row["tenant_id"] == "acme", "tenant do client gửi đã lọt vào sổ ca"


# ── Bề mặt Telegram: handle_hitl_callback ─────────────────────────────────────


class _FakeKafka:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_dict(self, topic, body, key=None):
        self.sent.append((topic, body))

    async def send_and_wait(self, topic, value=None, key=None):
        self.sent.append((topic, {"value": value}))


class _FakeTelegram:
    def __init__(self) -> None:
        self.acks: list[tuple[str, str]] = []

    async def answer_callback_query(self, cq_id, *, text=None, show_alert=False):
        self.acks.append((cq_id, text or ""))
        return {}


def _tg_ctx(redis, pool):
    return SimpleNamespace(
        redis=redis, kafka=_FakeKafka(), telegram=_FakeTelegram(), admin_pool=pool,
        settings=SimpleNamespace(
            kafka_topic_audit_chain="omni-audit-chain",
            kafka_topic_actions="omni-actions",
            kafka_topic_action_feedback="omni-action-feedback",
        ),
    )


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


async def test_telegram_reject_lands_as_incorrect(redis):
    from workers.hitl_telegram import handle_hitl_callback, pending_key

    await redis.set(pending_key("tg-1"), json.dumps({
        "trace_id": "trace-9", "tool_name": "k8s_delete_pod",
        "lane": "SIEM_SECURITY", "alertname": "SuspiciousExec", "tenant_id": "acme",
    }))
    pool = _FakePool()
    ctx = _tg_ctx(redis, pool)

    consumed = await handle_hitl_callback(
        ctx, {"callback_query": {"id": "cq1", "data": "hitl:reject:tg-1", "from": {"id": 42}}}
    )

    assert consumed is True
    row = pool.rows[case_id_for_hitl(pending_id="tg-1", trace_id="trace-9")]
    assert row["diagnosis_verdict"] == "INCORRECT"
    assert row["diagnosis_source"] == "hitl"
    assert row["tenant_id"] == "acme"
    # pattern_key đóng băng theo LOẠI VIỆC — cơ sở để xin quyền theo từng pattern.
    assert row["pattern_key"] == "SIEM_SECURITY:SuspiciousExec:k8s_delete_pod"


async def test_telegram_approve_lands_as_correct(redis):
    from workers.hitl_telegram import handle_hitl_callback, pending_key

    await redis.set(pending_key("tg-2"), json.dumps({
        "trace_id": "trace-10", "tool_name": "k8s_rollout_restart",
        "action_body": {"tool_name": "k8s_rollout_restart"},
    }))
    pool = _FakePool()
    ctx = _tg_ctx(redis, pool)

    await handle_hitl_callback(
        ctx, {"callback_query": {"id": "cq2", "data": "hitl:approve:tg-2", "from": {"id": 7}}}
    )

    row = pool.rows[case_id_for_hitl(pending_id="tg-2", trace_id="trace-10")]
    assert row["diagnosis_verdict"] == "CORRECT"
    assert row["remedy_verdict"] == "UNJUDGED"
    assert "omni-actions" in [t for t, _ in ctx.kafka.sent]


async def test_telegram_decision_survives_postgres_outage(redis):
    """PG chết: action vẫn dispatch, operator vẫn được ack. Không im lặng nuốt lệnh."""
    from workers.hitl_telegram import handle_hitl_callback, pending_key

    await redis.set(pending_key("tg-3"), json.dumps({
        "trace_id": "t3", "tool_name": "k8s_scale_resource",
        "action_body": {"tool_name": "k8s_scale_resource"},
    }))
    ctx = _tg_ctx(redis, _BrokenPool())

    consumed = await handle_hitl_callback(
        ctx, {"callback_query": {"id": "cq3", "data": "hitl:approve:tg-3", "from": {"id": 1}}}
    )

    assert consumed is True
    assert "omni-actions" in [t for t, _ in ctx.kafka.sent]
    assert ctx.telegram.acks and "duyệt" in ctx.telegram.acks[0][1]
    assert await redis.get(pending_key("tg-3")) is None


async def test_second_verdict_does_not_reopen_case():
    """Ca đã tồn tại thì KHÔNG mở lại — mở lại là đường vòng để đổi pattern_key."""
    pool = _FakePool()
    for _ in range(2):
        await record_hitl_verdict(
            pool=pool, tenant_id="acme", pending_id="p-9", decision="REJECTED",
            actor="sre", tool_name="k8s_delete_pod",
        )
    assert len(pool.rows) == 1
    row = pool.rows[case_id_for_hitl(pending_id="p-9")]
    assert row["occurrence_no"] == 1


async def test_unknown_decision_is_ignored():
    pool = _FakePool()
    assert await record_hitl_verdict(
        pool=pool, tenant_id="acme", pending_id="p-x", decision="MAYBE", actor="sre",
    ) is None
    assert pool.rows == {}
