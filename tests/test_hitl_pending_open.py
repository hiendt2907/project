"""#27/#28 — HITL tier-gate phải mở pending thật, không chỉ skip im lặng.

Trước bản vá: ``gate_decision_for_tool`` trả "HITL" chỉ dẫn tới
skip + action_feedback — không nơi nào tạo cơ hội cho người duyệt
(``build_hitl_card`` mồ côi, ``omni_admin.hitl_decision`` không bao giờ có
INSERT gốc). Test này khoá lại: quyết định HITL phải mở pending thật (CRAT +
Postgres + Redis + Telegram), và mở fail-closed đúng theo CRAT trước.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from services.audit_ledger.signer import AuditLedgerError
from workers.hitl_telegram import open_hitl_pending_for_mutate, pending_key


class _FakeKafka:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_dict(self, topic, body, key=None):
        self.sent.append((topic, body))


class _FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict | None]] = []

    async def send_message(self, chat_id, text, *, reply_markup=None, parse_mode=None):
        self.messages.append((chat_id, text, reply_markup))
        return {}


class _FakeAdminRepo:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create_hitl_pending(self, **kwargs):
        self.created.append(kwargs)


def _ctx(redis, *, admin_repo=None, chat_id=999):
    return SimpleNamespace(
        redis=redis,
        kafka=_FakeKafka(),
        telegram=_FakeTelegram(),
        admin_repo=admin_repo if admin_repo is not None else _FakeAdminRepo(),
        settings=SimpleNamespace(
            kafka_topic_audit_chain="omni-audit-chain",
            telegram_admin_chat_id=chat_id,
        ),
    )


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


async def _fake_write_audit_block_ok(**kwargs):
    return {"seq": 1, "signature_hex": "ab"}


async def test_open_hitl_pending_writes_postgres_redis_and_telegram(monkeypatch, redis):
    monkeypatch.setattr(
        "workers.hitl_telegram.write_audit_block", AsyncMock(side_effect=_fake_write_audit_block_ok)
    )
    ctx = _ctx(redis)

    await open_hitl_pending_for_mutate(
        ctx, trace="trace-1", tenant_id="acme", tool_name="k8s_delete_pod",
        args={"namespace": "multi-agent", "pod": "flaky-pod"},
        risk_class="HIGH", tier="auto",
    )

    pending_id = "mut-trace-1"
    # Postgres: pending row thật đã được INSERT — trước bản vá, không bao giờ xảy ra.
    assert ctx.admin_repo.created == [{
        "pending_id": pending_id, "tenant_id": "acme", "tool_name": "k8s_delete_pod",
        "risk_class": "HIGH", "tier_at_time": "auto",
    }]
    # Redis: payload có action_body để handle_hitl_callback dispatch lại nếu approve.
    raw = await redis.get(pending_key(pending_id))
    assert raw is not None
    payload = json.loads(raw)
    assert payload["action_body"]["data"]["tool_name"] == "k8s_delete_pod"
    assert payload["action_body"]["data"]["args"]["pod"] == "flaky-pod"
    # Telegram: card thật được gửi, callback_data trỏ đúng pending_id (trước bản vá
    # build_hitl_card không có caller nào — không ai từng gửi card này).
    assert len(ctx.telegram.messages) == 1
    chat_id, text, reply_markup = ctx.telegram.messages[0]
    assert chat_id == 999
    assert pending_id in text
    callback_datas = [
        btn["callback_data"]
        for row in reply_markup["inline_keyboard"]
        for btn in row
    ]
    assert f"hitl:approve:{pending_id}" in callback_datas
    assert f"hitl:reject:{pending_id}" in callback_datas


async def test_open_hitl_pending_aborts_fail_closed_on_crat_failure(monkeypatch, redis):
    """CRAT fail-closed: nếu audit chain không ghi được, KHÔNG được mở pending nào
    cả (không Postgres, không Redis, không Telegram) — giống pattern evidence_consumer.py."""
    async def _fail(**kwargs):
        raise AuditLedgerError("redis down")

    monkeypatch.setattr("workers.hitl_telegram.write_audit_block", AsyncMock(side_effect=_fail))
    ctx = _ctx(redis)

    await open_hitl_pending_for_mutate(
        ctx, trace="trace-2", tenant_id="acme", tool_name="k8s_delete_pod",
        args={}, risk_class="HIGH", tier="auto",
    )

    assert ctx.admin_repo.created == []
    assert await redis.get(pending_key("mut-trace-2")) is None
    assert ctx.telegram.messages == []


async def test_open_hitl_pending_survives_missing_admin_repo(monkeypatch, redis):
    """admin_repo=None (lab chưa cấu hình OMNI_ADMIN_PG_DSN) không được chặn CRAT
    + Redis + Telegram — ledger Postgres là phụ trợ, không phải đường quyết định."""
    monkeypatch.setattr(
        "workers.hitl_telegram.write_audit_block", AsyncMock(side_effect=_fake_write_audit_block_ok)
    )
    ctx = _ctx(redis, admin_repo=None)

    await open_hitl_pending_for_mutate(
        ctx, trace="trace-3", tenant_id="acme", tool_name="k8s_delete_pod",
        args={}, risk_class="HIGH", tier="auto",
    )

    assert await redis.get(pending_key("mut-trace-3")) is not None
    assert len(ctx.telegram.messages) == 1
