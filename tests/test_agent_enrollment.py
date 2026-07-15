"""IT-3 sprint "Nhân viên SRE" — enrollment + identity per-agent.

Kiểm chứng contract của plan:
  - enroll 2 lần cùng token → lần 2 bị từ chối (single-use, atomic UPDATE)
  - agent revoked → 401 ngay (auth-cache bị DEL)
  - token hết hạn → từ chối
  - per-agent credential đi qua _require_api_key → TenantContext đúng tenant,
    KHÔNG admin

Fake PG pool tự chứa (query-substring routing như test_admin_config_store.py);
FakeRedis cho auth-cache; gateway route test qua ASGITransport.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

import fakeredis.aioredis
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from services.admin_config.repo import AdminConfigRepo


# ── Fake asyncpg pool (chỉ các query của enrollment flow) ─────────────────────
class _Row(dict):
    pass


class _Store:
    def __init__(self) -> None:
        self.tenant: dict[str, dict[str, Any]] = {}
        self.environment: dict[tuple[str, str], dict[str, Any]] = {}
        self.enroll_token: dict[int, dict[str, Any]] = {}
        self.credential: dict[int, dict[str, Any]] = {}
        self.plan: dict[str, dict[str, Any]] = {
            "staging-sim": {"agent_limit": 10, "enabled": True},
        }
        self.audit: list[tuple[str, tuple]] = []
        self._id = 0

    def next_id(self) -> int:
        self._id += 1
        return self._id


class _FakeTx:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def __aenter__(self) -> "_FakeTx":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, store: _Store) -> None:
        self._s = store

    def transaction(self) -> _FakeTx:
        return _FakeTx(self._s)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        s = self._s
        if "SELECT 1 FROM omni_admin.tenant WHERE tenant_id" in sql:
            return 1 if args[0] in s.tenant else None
        if "INSERT INTO omni_admin.agent_enroll_token" in sql:
            tid = s.next_id()
            tenant, environment_id, token_hash, token_prefix, label, actor, expires_at = args
            s.enroll_token[tid] = {
                "id": tid, "tenant_id": tenant, "token_hash": token_hash,
                "environment_id": environment_id,
                "token_prefix": token_prefix, "label": label, "status": "issued",
                "created_by": actor, "expires_at": expires_at,
                "used_at": None, "used_by_agent": None,
            }
            return tid
        if "INSERT INTO omni_admin.agent_credential" in sql:
            cid = s.next_id()
            tenant, environment_id, agent_id, hostname, key_hash, key_prefix, token_id = args
            s.credential[cid] = {
                "id": cid, "tenant_id": tenant, "agent_id": agent_id,
                "environment_id": environment_id,
                "hostname": hostname, "key_hash": key_hash,
                "key_prefix": key_prefix, "status": "active",
                "enrolled_via_token": token_id, "created_at": None, "revoked_at": None,
            }
            return cid
        if "SELECT COUNT(*) FROM omni_admin.agent_credential" in sql:
            tenant, agent_id = args
            return sum(1 for rec in s.credential.values()
                       if rec["tenant_id"] == tenant and rec["agent_id"] != agent_id
                       and rec["status"] == "active")
        raise AssertionError(f"fetchval chưa hỗ trợ: {sql[:80]}")

    async def fetchrow(self, sql: str, *args: Any) -> _Row | None:
        s = self._s
        if "SELECT status FROM omni_admin.environment" in sql:
            rec = s.environment.get((args[0], args[1]))
            return _Row({"status": rec["status"]}) if rec else None
        if "FROM omni_admin.tenant_plan" in sql:
            plan = s.plan.get(args[0])
            return _Row(plan) if plan else None
        if "UPDATE omni_admin.agent_enroll_token" in sql and "RETURNING id, tenant_id" in sql:
            token_hash, agent_id = args
            now = datetime.now(timezone.utc)
            for rec in s.enroll_token.values():
                expires = rec["expires_at"]
                if (rec["token_hash"] == token_hash and rec["status"] == "issued"
                        and (expires is None or expires > now)):
                    rec["status"] = "used"
                    rec["used_by_agent"] = agent_id
                    return _Row({"id": rec["id"], "tenant_id": rec["tenant_id"],
                                 "environment_id": rec.get("environment_id")})
            return None
        if "SELECT tenant_id, agent_id" in sql and "FROM omni_admin.agent_credential" in sql:
            for rec in s.credential.values():
                if rec["key_hash"] == args[0] and rec["status"] == "active":
                    return _Row({"tenant_id": rec["tenant_id"], "agent_id": rec["agent_id"],
                                 "environment_id": rec.get("environment_id")})
            return None
        raise AssertionError(f"fetchrow chưa hỗ trợ: {sql[:80]}")

    async def fetch(self, sql: str, *args: Any) -> list[_Row]:
        s = self._s
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
            return [
                _Row(rec) for rec in s.credential.values() if rec["tenant_id"] == args[0]
            ]
        raise AssertionError(f"fetch chưa hỗ trợ: {sql[:80]}")

    async def execute(self, sql: str, *args: Any) -> None:
        s = self._s
        if "UPDATE omni_admin.agent_credential" in sql and "status='revoked'" in sql:
            tenant, agent_id = args
            for rec in s.credential.values():
                if (rec["tenant_id"] == tenant and rec["agent_id"] == agent_id
                        and rec["status"] == "active"):
                    rec["status"] = "revoked"
            return
        if "INSERT INTO omni_admin.config_change_log" in sql:
            s.audit.append(("config_change_log", args))
            return
        if "INSERT INTO omni_admin.crat_outbox" in sql:
            s.audit.append(("crat_outbox", args))
            return
        raise AssertionError(f"execute chưa hỗ trợ: {sql[:80]}")


class _Acquire:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._s)

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakePool:
    def __init__(self, store: _Store) -> None:
        self._s = store

    def acquire(self) -> _Acquire:
        return _Acquire(self._s)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def store() -> _Store:
    s = _Store()
    s.tenant["staging-sim"] = {"tenant_id": "staging-sim", "status": "active"}
    return s


@pytest.fixture
def repo(store: _Store) -> AdminConfigRepo:
    return AdminConfigRepo(_FakePool(store))


# ── Repo layer ────────────────────────────────────────────────────────────────
class TestEnrollTokenRepo:
    async def test_create_token_requires_existing_tenant(self, repo):
        with pytest.raises(ValueError, match="không tồn tại"):
            await repo.create_enroll_token(
                tenant_id="ghost", token_hash=_sha("t"), token_prefix="t",
                actor="test",
            )

    async def test_consume_token_issues_credential_once(self, repo):
        await repo.create_enroll_token(
            tenant_id="staging-sim", token_hash=_sha("tok-1"), token_prefix="tok-1",
            actor="test",
        )
        first = await repo.consume_enroll_token_and_issue_credential(
            token_hash=_sha("tok-1"), agent_id="cust-app-agent", hostname="cust-app",
            key_hash=_sha("key-1"), key_prefix="key-1",
        )
        assert first is not None
        assert first["tenant_id"] == "staging-sim"
        # lần 2 cùng token → None (single-use)
        second = await repo.consume_enroll_token_and_issue_credential(
            token_hash=_sha("tok-1"), agent_id="cust-app-agent", hostname="cust-app",
            key_hash=_sha("key-2"), key_prefix="key-2",
        )
        assert second is None

    async def test_environment_bound_token_returns_environment_identity(self, repo, store):
        store.environment[("staging-sim", "prod")] = {"status": "active"}
        await repo.create_enroll_token(
            tenant_id="staging-sim", environment_id="prod",
            token_hash=_sha("tok-prod"), token_prefix="tok-prod", actor="test",
        )
        result = await repo.consume_enroll_token_and_issue_credential(
            token_hash=_sha("tok-prod"), agent_id="prod-agent", hostname="prod-1",
            key_hash=_sha("key-prod"), key_prefix="key-prod",
        )
        assert result["environment_id"] == "prod"
        assert await repo.lookup_agent_credential(_sha("key-prod")) == {
            "tenant_id": "staging-sim", "agent_id": "prod-agent", "environment_id": "prod",
        }

    async def test_expired_token_rejected(self, repo):
        await repo.create_enroll_token(
            tenant_id="staging-sim", token_hash=_sha("tok-exp"), token_prefix="tok-exp",
            actor="test",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        result = await repo.consume_enroll_token_and_issue_credential(
            token_hash=_sha("tok-exp"), agent_id="a", hostname="h",
            key_hash=_sha("k"), key_prefix="k",
        )
        assert result is None

    async def test_reenroll_revokes_previous_credential(self, repo, store):
        for i in (1, 2):
            await repo.create_enroll_token(
                tenant_id="staging-sim", token_hash=_sha(f"tok-{i}"),
                token_prefix=f"tok-{i}", actor="test",
            )
            await repo.consume_enroll_token_and_issue_credential(
                token_hash=_sha(f"tok-{i}"), agent_id="cust-app-agent", hostname="cust-app",
                key_hash=_sha(f"key-{i}"), key_prefix=f"key-{i}",
            )
        # key cũ bị revoke, key mới active
        assert await repo.lookup_agent_credential(_sha("key-1")) is None
        rec = await repo.lookup_agent_credential(_sha("key-2"))
        assert rec == {"tenant_id": "staging-sim", "agent_id": "cust-app-agent",
                       "environment_id": None}

    async def test_plan_agent_limit_blocks_new_agent_but_allows_reenroll(self, repo, store):
        store.plan["staging-sim"]["agent_limit"] = 1
        await repo.create_enroll_token(tenant_id="staging-sim", token_hash=_sha("tok-l1"),
                                       token_prefix="tok-l1", actor="test")
        await repo.consume_enroll_token_and_issue_credential(
            token_hash=_sha("tok-l1"), agent_id="agent-1", hostname="h1",
            key_hash=_sha("key-l1"), key_prefix="key-l1")
        await repo.create_enroll_token(tenant_id="staging-sim", token_hash=_sha("tok-l2"),
                                       token_prefix="tok-l2", actor="test")
        with pytest.raises(ValueError, match="giới hạn agent"):
            await repo.consume_enroll_token_and_issue_credential(
                token_hash=_sha("tok-l2"), agent_id="agent-2", hostname="h2",
                key_hash=_sha("key-l2"), key_prefix="key-l2")

    async def test_revoke_returns_hashes_and_lookup_dies(self, repo):
        await repo.create_enroll_token(
            tenant_id="staging-sim", token_hash=_sha("tok-r"), token_prefix="tok-r",
            actor="test",
        )
        await repo.consume_enroll_token_and_issue_credential(
            token_hash=_sha("tok-r"), agent_id="cust-db-agent", hostname="cust-db",
            key_hash=_sha("key-r"), key_prefix="key-r",
        )
        hashes = await repo.revoke_agent_credentials(
            tenant_id="staging-sim", agent_id="cust-db-agent", actor="test",
        )
        assert hashes == [_sha("key-r")]
        assert await repo.lookup_agent_credential(_sha("key-r")) is None

    async def test_audit_written_for_enroll(self, repo, store):
        await repo.create_enroll_token(
            tenant_id="staging-sim", token_hash=_sha("tok-a"), token_prefix="tok-a",
            actor="test",
        )
        await repo.consume_enroll_token_and_issue_credential(
            token_hash=_sha("tok-a"), agent_id="a1", hostname="h1",
            key_hash=_sha("k1"), key_prefix="k1",
        )
        tables = [t for t, _ in store.audit]
        assert tables.count("config_change_log") >= 2  # token create + credential create
        assert tables.count("crat_outbox") >= 2


# ── Gateway enroll route ──────────────────────────────────────────────────────
def _enroll_app(repo: AdminConfigRepo, redis: Any) -> FastAPI:
    from gateway.routes.agent_enroll import router

    app = FastAPI()
    app.include_router(router)
    app.state.admin_repo = repo
    app.state.redis = redis
    return app


class TestEnrollEndpoint:
    async def test_enroll_twice_same_token_second_rejected(self, repo):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        raw_token = "enroll-token-plaintext-0123456789"
        await repo.create_enroll_token(
            tenant_id="staging-sim", token_hash=_sha(raw_token), token_prefix=raw_token[:8],
            actor="test",
        )
        app = _enroll_app(repo, redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r1 = await c.post("/webhook/agent/enroll", json={
                "enroll_token": raw_token, "agent_id": "cust-app-agent",
                "hostname": "cust-app",
            })
            r2 = await c.post("/webhook/agent/enroll", json={
                "enroll_token": raw_token, "agent_id": "cust-app-agent",
                "hostname": "cust-app",
            })
        assert r1.status_code == 201
        body = r1.json()
        assert body["tenant_id"] == "staging-sim"
        assert body["api_key"]  # plaintext trả đúng 1 lần
        assert r2.status_code == 401

    async def test_enroll_unknown_token_401(self, repo):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        app = _enroll_app(repo, redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/webhook/agent/enroll", json={
                "enroll_token": "never-issued-token-000000", "agent_id": "x",
            })
        assert r.status_code == 401

    async def test_enroll_rate_limited_per_ip(self, repo):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        app = _enroll_app(repo, redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            last = None
            for _ in range(11):
                last = await c.post("/webhook/agent/enroll", json={
                    "enroll_token": "never-issued-token-000000", "agent_id": "x",
                })
        assert last is not None and last.status_code == 429


# ── _require_api_key: per-agent credential fallback ───────────────────────────
def _guarded_app(repo: AdminConfigRepo, redis: Any) -> FastAPI:
    from gateway.api import _require_api_key

    app = FastAPI()
    app.state.admin_repo = repo
    app.state.redis = redis

    @app.get("/protected", dependencies=[Depends(_require_api_key)])
    async def protected(request: Any = None):  # pragma: no cover - body trivial
        return {"ok": True}

    return app


class TestPerAgentCredentialAuth:
    async def test_agent_credential_accepted_and_scoped(self, repo, monkeypatch):
        monkeypatch.setenv("OMNI_ADMIN_API_KEYS", "admin-secret-key")
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        raw_token = "enroll-token-auth-0123456789"
        await repo.create_enroll_token(
            tenant_id="staging-sim", token_hash=_sha(raw_token), token_prefix="pfx",
            actor="test",
        )
        result = await repo.consume_enroll_token_and_issue_credential(
            token_hash=_sha(raw_token), agent_id="cust-app-agent", hostname="cust-app",
            key_hash=_sha("agent-plain-key"), key_prefix="agent-pl",
        )
        assert result is not None
        app = _guarded_app(repo, redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/protected", headers={"Authorization": "Bearer agent-plain-key"})
        assert r.status_code == 200
        # positive-hit đã được cache
        cached = await redis.get(f"omni:agentcred:cache:{_sha('agent-plain-key')}")
        assert cached and "staging-sim" in cached

    async def test_revoked_agent_401_immediately(self, repo, monkeypatch):
        monkeypatch.setenv("OMNI_ADMIN_API_KEYS", "admin-secret-key")
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        raw_token = "enroll-token-revoke-0123456789"
        await repo.create_enroll_token(
            tenant_id="staging-sim", token_hash=_sha(raw_token), token_prefix="pfx",
            actor="test",
        )
        await repo.consume_enroll_token_and_issue_credential(
            token_hash=_sha(raw_token), agent_id="cust-db-agent", hostname="cust-db",
            key_hash=_sha("revoke-me-key"), key_prefix="revoke-m",
        )
        app = _guarded_app(repo, redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            ok = await c.get("/protected", headers={"Authorization": "Bearer revoke-me-key"})
            assert ok.status_code == 200
            # revoke + DEL cache (mô phỏng autonomy.revoke_agent_credentials)
            hashes = await repo.revoke_agent_credentials(
                tenant_id="staging-sim", agent_id="cust-db-agent", actor="test",
            )
            await redis.delete(*[f"omni:agentcred:cache:{h}" for h in hashes])
            denied = await c.get("/protected", headers={"Authorization": "Bearer revoke-me-key"})
        assert denied.status_code == 401

    async def test_wrong_key_still_401(self, repo, monkeypatch):
        monkeypatch.setenv("OMNI_ADMIN_API_KEYS", "admin-secret-key")
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        app = _guarded_app(repo, redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/protected", headers={"Authorization": "Bearer no-such-key"})
        assert r.status_code == 401


# ── Admin gate: agent credential KHÔNG được phát token / revoke ───────────────
class TestEnrollTokenAdminGate:
    async def test_agent_credential_cannot_issue_enroll_token(self, repo, monkeypatch):
        from gateway.api import _require_api_key
        from gateway.routes.autonomy import router as autonomy_router

        monkeypatch.setenv("OMNI_ADMIN_API_KEYS", "admin-secret-key")
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        raw_token = "enroll-token-gate-0123456789"
        await repo.create_enroll_token(
            tenant_id="staging-sim", token_hash=_sha(raw_token), token_prefix="pfx",
            actor="test",
        )
        await repo.consume_enroll_token_and_issue_credential(
            token_hash=_sha(raw_token), agent_id="a-gate", hostname="h",
            key_hash=_sha("agent-gate-key"), key_prefix="agent-ga",
        )
        app = FastAPI()
        app.include_router(autonomy_router, dependencies=[Depends(_require_api_key)])
        app.state.admin_repo = repo
        app.state.redis = redis
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            # per-agent credential → 403 (không phải admin)
            denied = await c.post(
                "/autonomy/tenants/staging-sim/enroll-tokens", json={},
                headers={"Authorization": "Bearer agent-gate-key"},
            )
            denied_revoke = await c.delete(
                "/autonomy/tenants/staging-sim/agent-credentials/a-gate",
                headers={"Authorization": "Bearer agent-gate-key"},
            )
            # admin key → OK
            allowed = await c.post(
                "/autonomy/tenants/staging-sim/enroll-tokens", json={},
                headers={"Authorization": "Bearer admin-secret-key"},
            )
        assert denied.status_code == 403
        assert denied_revoke.status_code == 403
        assert allowed.status_code == 200
        assert allowed.json()["enroll_token"]


# ── AOIP enrollment client ────────────────────────────────────────────────────
class TestAoipEnrollmentClient:
    async def test_enroll_agent_happy_path_and_rejection(self, repo):
        from aoip.agent.enrollment import EnrollmentError, enroll_agent

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        raw_token = "enroll-token-client-0123456789"
        await repo.create_enroll_token(
            tenant_id="staging-sim", token_hash=_sha(raw_token), token_prefix="pfx",
            actor="test",
        )
        app = _enroll_app(repo, redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            result = await enroll_agent(
                "http://t", enroll_token=raw_token,
                agent_id="cust-app-agent", hostname="cust-app", client=c,
            )
            assert result.tenant_id == "staging-sim"
            assert result.api_key and result.key_prefix == result.api_key[:8]
            with pytest.raises(EnrollmentError) as exc:
                await enroll_agent(
                    "http://t", enroll_token=raw_token,
                    agent_id="cust-app-agent", hostname="cust-app", client=c,
                )
            assert exc.value.status_code == 401
