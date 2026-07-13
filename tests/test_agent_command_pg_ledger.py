"""IT-6: command outcome durability — PG ledger cho durable command channel.

Bất biến chứng minh: mỗi (tenant, command_id) có ĐÚNG MỘT terminal outcome trong PG,
qua cả 3 đường ghi (hot-path terminal, expire, reconcile startup), kể cả khi:
- agent report terminal nhiều lần (duplicate delivery / retry),
- PG down đúng lúc report (reconciler backfill từ Redis),
- gateway restart giữa chừng (reconcile idempotent, không ghi đè outcome cũ).

Fake pool mô phỏng asyncpg đúng semantics của 2 câu SQL trong ledger.py
(INSERT ON CONFLICT DO NOTHING + UPSERT first-writer-wins WHERE terminal_at IS NULL).
Chaos proof trên K8s/VM thật: scripts trong PRODUCT_PROOF Iteration 32.
"""
from __future__ import annotations

import json
import time
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from services.agent_command_ledger import (
    pg_record_enqueue,
    pg_record_terminal,
    reconcile_commands_from_redis,
)

TENANT = "acme"
AGENT = "agent-rt-1"


# ── Fake asyncpg pool ────────────────────────────────────────────────────────

class _FakeConn:
    def __init__(self, store: dict) -> None:
        self._store = store  # key=(tenant, command_id) → row dict

    @staticmethod
    def _row(args: tuple) -> dict:
        cols = ["tenant_id", "command_id", "agent_id", "mission_id", "incident_id",
                "decision_id", "action_id", "canonical_scope", "payload_hash",
                "state", "outcome", "delivery_attempt", "created_at", "terminal_at",
                "source"]
        return dict(zip(cols, args))

    async def execute(self, sql: str, *args: Any) -> None:
        assert "ON CONFLICT (tenant_id, command_id) DO NOTHING" in sql
        row = self._row(args)
        self._store.setdefault((row["tenant_id"], row["command_id"]), row)

    async def fetchrow(self, sql: str, *args: Any) -> dict | None:
        assert "DO UPDATE SET" in sql and "terminal_at IS NULL" in sql
        row = self._row(args)
        key = (row["tenant_id"], row["command_id"])
        existing = self._store.get(key)
        if existing is None or existing.get("terminal_at") is None:
            self._store[key] = row
            return {"state": row["state"]}
        return None  # first-writer-wins: đã terminal → WHERE loại, RETURNING rỗng


class _Acquire:
    def __init__(self, store: dict) -> None:
        self._store = store

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._store)

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self.rows: dict = {}

    def acquire(self) -> _Acquire:
        return _Acquire(self.rows)


class _BrokenPool:
    def acquire(self):  # noqa: ANN201 — mô phỏng PG down
        raise ConnectionError("pg down")


def _rec(command_id="cmd-1", state="COMPLETED", terminal=True) -> dict:
    now = int(time.time())
    return {"command_id": command_id, "tenant_id": TENANT, "agent_id": AGENT,
            "mission_id": "mis-1", "incident_id": "inc-1", "decision_id": "dec-1",
            "action_id": "act-1", "canonical_scope": f"{TENANT}:svc:db",
            "payload_hash": "ph-1", "payload": {"verb": "restart"},
            "created_at": now - 10, "expires_at": now + 300, "state": state,
            "delivery_attempt": 1, "terminal_at": now if terminal else 0,
            "outcome": {"result": "updated"} if terminal else {}}


# ── Unit: ledger semantics ───────────────────────────────────────────────────

async def test_terminal_recorded_exactly_once():
    pool = _FakePool()
    assert await pg_record_terminal(pool, _rec()) == "recorded"
    # duplicate report (retry/duplicate delivery) → không ghi đè
    dup = _rec()
    dup["outcome"] = {"result": "DIFFERENT"}
    assert await pg_record_terminal(pool, dup) == "already_terminal"
    row = pool.rows[(TENANT, "cmd-1")]
    assert row["state"] == "COMPLETED"
    assert json.loads(row["outcome"]) == {"result": "updated"}
    assert len(pool.rows) == 1


async def test_terminal_skips_non_terminal_state_and_none_pool():
    pool = _FakePool()
    assert await pg_record_terminal(pool, _rec(state="RUNNING", terminal=False)) == "skipped"
    assert await pg_record_terminal(None, _rec()) == "skipped"
    assert pool.rows == {}


async def test_enqueue_then_terminal_upgrades_same_row():
    pool = _FakePool()
    assert await pg_record_enqueue(pool, _rec(state="QUEUED", terminal=False)) is True
    assert pool.rows[(TENANT, "cmd-1")]["terminal_at"] is None
    assert await pg_record_terminal(pool, _rec(state="FAILED")) == "recorded"
    assert pool.rows[(TENANT, "cmd-1")]["state"] == "FAILED"
    assert len(pool.rows) == 1


async def test_pg_down_is_best_effort_not_exception():
    assert await pg_record_enqueue(_BrokenPool(), _rec(state="QUEUED", terminal=False)) is False
    assert await pg_record_terminal(_BrokenPool(), _rec()) == "error"


# ── Unit: reconcile Redis → PG (safety net startup) ──────────────────────────

async def test_reconcile_backfills_terminal_missing_in_pg():
    redis = FakeRedis(decode_responses=True)
    pool = _FakePool()
    await redis.set(f"omni:cmd:rec:{TENANT}:cmd-1", json.dumps(_rec("cmd-1")))
    await redis.set(f"omni:cmd:rec:{TENANT}:cmd-2",
                    json.dumps(_rec("cmd-2", state="RUNNING", terminal=False)))
    counts = await reconcile_commands_from_redis(pool, redis)
    assert counts["scanned"] == 2 and counts["recorded"] == 1 and counts["inserted_open"] == 1
    assert pool.rows[(TENANT, "cmd-1")]["state"] == "COMPLETED"
    assert pool.rows[(TENANT, "cmd-2")]["terminal_at"] is None


async def test_reconcile_idempotent_does_not_overwrite_existing_outcome():
    redis = FakeRedis(decode_responses=True)
    pool = _FakePool()
    await pg_record_terminal(pool, _rec("cmd-1"))
    changed = _rec("cmd-1", state="FAILED")
    await redis.set(f"omni:cmd:rec:{TENANT}:cmd-1", json.dumps(changed))
    counts = await reconcile_commands_from_redis(pool, redis)
    assert counts["already_terminal"] == 1 and counts["recorded"] == 0
    assert pool.rows[(TENANT, "cmd-1")]["state"] == "COMPLETED"


# ── Integration: qua REAL gateway route (ASGITransport) ──────────────────────

def _make_app(redis, pool) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis
    app.state.admin_pool = pool
    from gateway.routes.agent_runtime import router
    app.include_router(router)
    return app


async def _register(redis):
    await redis.set(f"omni:remote_agent:registry:{AGENT}",
                    json.dumps({"agent_id": AGENT, "tenant_id": TENANT,
                                "last_seen": int(time.time())}))


def _cmd(command_id="cmd-1", ttl_s=300):
    return {"command_id": command_id, "agent_id": AGENT, "tenant_id": TENANT,
            "mission_id": "mis-1", "incident_id": "inc-1", "decision_id": "dec-1",
            "action_id": "act-1", "canonical_scope": f"{TENANT}:svc:db",
            "payload_hash": "ph-1", "payload": {"verb": "restart"}, "ttl_s": ttl_s}


async def test_http_enqueue_and_terminal_write_exactly_one_pg_outcome():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    pool = _FakePool()
    app = _make_app(redis, pool)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())).status_code == 200
        assert pool.rows[(TENANT, "cmd-1")]["state"] == "QUEUED"
        cmds = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"]
        tok = cmds[0]["fencing_token"]
        term = {"agent_id": AGENT, "tenant_id": TENANT, "command_id": "cmd-1",
                "delivery_attempt": 1, "fencing_token": tok,
                "state": "COMPLETED", "outcome": {"result": "updated"}}
        r = await c.post("/webhook/agent/rt/commands/terminal", json=term)
        assert r.status_code == 200 and r.json()["acknowledged"] is True
        # idempotent re-report — vẫn đúng 1 outcome PG
        r2 = await c.post("/webhook/agent/rt/commands/terminal", json=term)
        assert r2.status_code == 200 and r2.json()["idempotent"] is True
        row = pool.rows[(TENANT, "cmd-1")]
        assert row["state"] == "COMPLETED" and row["terminal_at"] is not None
        assert len(pool.rows) == 1


async def test_http_pg_down_during_terminal_then_reconcile_backfills():
    """Chaos shape (CI): PG down lúc agent report terminal → ACK vẫn 200 (Redis
    durable), PG thiếu hàng; reconcile lúc gateway restart backfill đúng 1 outcome."""
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis, _BrokenPool())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        cmds = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"]
        term = {"agent_id": AGENT, "tenant_id": TENANT, "command_id": "cmd-1",
                "delivery_attempt": 1, "fencing_token": cmds[0]["fencing_token"],
                "state": "COMPLETED", "outcome": {"result": "updated"}}
        r = await c.post("/webhook/agent/rt/commands/terminal", json=term)
        assert r.status_code == 200 and r.json()["acknowledged"] is True  # không chặn agent
    # "gateway restart" — PG sống lại, reconcile từ Redis
    pool = _FakePool()
    counts = await reconcile_commands_from_redis(pool, redis)
    assert counts["recorded"] == 1
    assert pool.rows[(TENANT, "cmd-1")]["state"] == "COMPLETED"
    assert len(pool.rows) == 1


async def test_http_claim_expired_mirrors_expired_outcome_to_pg():
    """Command hết hạn TRƯỚC khi agent claim: Lua set EXPIRED trong Redis →
    poll_commands mirror sang PG (đường expire không đi qua /terminal)."""
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    pool = _FakePool()
    app = _make_app(redis, pool)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd("cmd-exp"))
        rkey = f"omni:cmd:rec:{TENANT}:cmd-exp"
        rec = json.loads(await redis.get(rkey))
        rec["expires_at"] = int(time.time()) - 5  # test-only: ép quá hạn
        await redis.set(rkey, json.dumps(rec))
        cmds = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"]
        assert cmds == []  # expired → không deliver
        row = pool.rows[(TENANT, "cmd-exp")]
        assert row["state"] == "EXPIRED" and row["terminal_at"] is not None
