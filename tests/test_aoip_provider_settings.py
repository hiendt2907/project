"""Provider Settings — agent enrollment/credential admin surfaced in-product.

Product step: /settings phải không còn là stub — operator issue enroll token
+ xem/revoke credential ngay trên portal thay vì curl tay vào Admin API IT-3
(docs/plans/sprint-agent-sre-employee-production.md IT-3).
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any

import fakeredis.aioredis as aioredis
import httpx

from aoip.console import identity
from aoip.console.app import create_provider_app
from aoip.console.settings import (
    build_provider_settings, issue_enroll_token, revoke_agent_credential,
)


# ── Fake asyncpg pool — chỉ các query mà settings.py thật sự chạy ─────────────
class _Row(dict):
    pass


class _Store:
    def __init__(self) -> None:
        self.tenant: dict[str, dict[str, Any]] = {}
        self.environment: dict[tuple[str, str], dict[str, Any]] = {}
        self.enroll_token: dict[int, dict[str, Any]] = {}
        self.credential: dict[int, dict[str, Any]] = {}
        self._id = 0

    def next_id(self) -> int:
        self._id += 1
        return self._id


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


class _FakeConn:
    def __init__(self, store: _Store) -> None:
        self._s = store

    def transaction(self) -> _FakeTx:
        return _FakeTx()

    async def fetchval(self, sql: str, *args: Any) -> Any:
        s = self._s
        if "SELECT 1 FROM omni_admin.tenant WHERE tenant_id" in sql:
            return 1 if args[0] in s.tenant else None
        if "INSERT INTO omni_admin.agent_enroll_token" in sql:
            tid = s.next_id()
            tenant, environment_id, token_hash, token_prefix, label, actor, expires_at = args
            s.enroll_token[tid] = {"id": tid, "tenant_id": tenant, "token_prefix": token_prefix}
            return tid
        raise AssertionError(f"fetchval chưa hỗ trợ: {sql[:80]}")

    async def fetchrow(self, sql: str, *args: Any) -> _Row | None:
        raise AssertionError(f"fetchrow chưa hỗ trợ: {sql[:80]}")

    async def fetch(self, sql: str, *args: Any) -> list[_Row]:
        s = self._s
        if "SELECT t.tenant_id, t.display_name" in sql:
            return [
                _Row({"tenant_id": tid, "display_name": rec.get("display_name") or tid,
                      "status": rec.get("status", "active"), "created_at": None,
                      "active_keys": 0})
                for tid, rec in s.tenant.items()
            ]
        if "FROM omni_admin.environment WHERE tenant_id = $1" in sql:
            return [_Row(rec) for rec in s.environment.values() if rec["tenant_id"] == args[0]]
        if "UPDATE omni_admin.agent_credential" in sql and "RETURNING id, key_hash" in sql:
            tenant, agent_id = args
            out = []
            for rec in s.credential.values():
                if (rec["tenant_id"] == tenant and rec["agent_id"] == agent_id
                        and rec["status"] == "active"):
                    rec["status"] = "revoked"
                    out.append(_Row({"id": rec["id"], "key_hash": rec["key_hash"]}))
            return out
        if "FROM omni_admin.agent_credential WHERE tenant_id = $1 ORDER BY id" in sql:
            return [_Row(rec) for rec in s.credential.values() if rec["tenant_id"] == args[0]]
        raise AssertionError(f"fetch chưa hỗ trợ: {sql[:80]}")

    async def execute(self, sql: str, *args: Any) -> None:
        if "INSERT INTO omni_admin.config_change_log" in sql:
            return
        if "INSERT INTO omni_admin.crat_outbox" in sql:
            return
        raise AssertionError(f"execute chưa hỗ trợ: {sql[:80]}")


class _Acquire:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._s)

    async def __aexit__(self, *exc):
        return None


class _FakePool:
    def __init__(self, store: _Store) -> None:
        self._s = store

    def acquire(self) -> _Acquire:
        return _Acquire(self._s)


def _seed_credential(store: _Store, *, tenant: str, agent_id: str, key_hash: str) -> None:
    cid = store.next_id()
    store.credential[cid] = {
        "id": cid, "tenant_id": tenant, "agent_id": agent_id, "hostname": "h",
        "environment_id": None,
        "key_hash": key_hash, "key_prefix": key_hash[:8], "status": "active",
        "created_at": None, "revoked_at": None,
    }


# ── settings.py functions, direct ─────────────────────────────────────────────
async def test_build_provider_settings_lists_tenants_and_credentials():
    store = _Store()
    store.tenant["acme"] = {"status": "active"}
    _seed_credential(store, tenant="acme", agent_id="acme-app", key_hash="deadbeef")
    pool = _FakePool(store)

    result = await build_provider_settings(pool)

    assert result["tenants"][0]["tenant_id"] == "acme"
    assert result["agent_credentials"]["acme"][0]["agent_id"] == "acme-app"


async def test_issue_enroll_token_returns_plaintext_once():
    store = _Store()
    store.tenant["acme"] = {"status": "active"}
    pool = _FakePool(store)

    result = await issue_enroll_token(
        pool, tenant_id="acme", actor="owner@aoip", label="cust-db pilot", ttl_seconds=3600,
    )

    assert result["tenant_id"] == "acme"
    assert len(result["enroll_token"]) > 20
    # PG never sees the plaintext — only its sha256 hash.
    stored = next(iter(store.enroll_token.values()))
    assert stored["token_prefix"] == result["enroll_token"][:8]


async def test_revoke_agent_credential_drops_redis_auth_cache():
    store = _Store()
    store.tenant["acme"] = {"status": "active"}
    _seed_credential(store, tenant="acme", agent_id="acme-app", key_hash="deadbeef")
    pool = _FakePool(store)
    redis = aioredis.FakeRedis(decode_responses=True)
    await redis.set("omni:agentcred:cache:deadbeef", "acme:acme-app")

    revoked = await revoke_agent_credential(
        pool, redis, tenant_id="acme", agent_id="acme-app", actor="owner@aoip",
    )

    assert revoked == 1
    assert await redis.get("omni:agentcred:cache:deadbeef") is None


# ── endpoint RBAC — platform_owner can mutate, provider_viewer cannot ────────
def _redis():
    return aioredis.FakeRedis(decode_responses=True)


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c")


async def test_settings_endpoints_enforce_change_policy_permission():
    r = _redis()
    await identity.upsert_user(r, subject="owner@aoip", email="owner@aoip")
    await identity.grant_provider_role(r, subject="owner@aoip", role="platform_owner")
    await identity.upsert_user(r, subject="viewer@aoip", email="viewer@aoip")
    await identity.grant_provider_role(r, subject="viewer@aoip", role="provider_viewer")

    store = _Store()
    store.tenant["acme"] = {"status": "active"}
    app = create_provider_app(r)
    app.state.pool = _FakePool(store)

    owner_p = await identity.resolve_provider_principal(r, "owner@aoip")
    owner_sid = (await identity.issue_session(r, principal=owner_p, now=time.time())).sid
    viewer_p = await identity.resolve_provider_principal(r, "viewer@aoip")
    viewer_sid = (await identity.issue_session(r, principal=viewer_p, now=time.time())).sid

    async with _client(app) as c:
        denied = await c.post(
            "/api/provider/v1/settings/enroll-tokens",
            json={"tenant_id": "acme"},
            headers={"Authorization": f"Bearer {viewer_sid}"},
        )
        assert denied.status_code == 403

        allowed = await c.post(
            "/api/provider/v1/settings/enroll-tokens",
            json={"tenant_id": "acme", "label": "pilot"},
            headers={"Authorization": f"Bearer {owner_sid}"},
        )
        assert allowed.status_code == 200
        assert len(allowed.json()["enroll_token"]) > 20

        listed = await c.get(
            "/api/provider/v1/settings", headers={"Authorization": f"Bearer {viewer_sid}"},
        )
        assert listed.status_code == 200
        assert listed.json()["tenants"][0]["tenant_id"] == "acme"
