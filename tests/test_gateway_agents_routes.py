"""Unit tests for src/gateway/routes/agents.py — new remote agent endpoints."""
from __future__ import annotations

import json
import time

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _make_app(redis=None) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis
    from gateway.routes.agents import router
    app.include_router(router)
    return app


def _reg_entry(agent_id: str, age_offset: int = 5) -> str:
    return json.dumps({
        "agent_id": agent_id,
        "hostname": f"host-{agent_id}",
        "last_seen": int(time.time()) - age_offset,
    })


# ── _get_redis 503 ────────────────────────────────────────────────────────────

class TestGetRedis:
    @pytest.mark.asyncio
    async def test_redis_none_returns_503_on_list_remote(self):
        app = _make_app(redis=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_redis_none_returns_503_on_eps(self):
        app = _make_app(redis=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote/eps")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_redis_none_returns_503_on_logs(self):
        app = _make_app(redis=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote/agent-001/logs")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_redis_none_returns_503_on_deregister(self):
        app = _make_app(redis=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete("/agents/remote/agent-001")
        assert resp.status_code == 503


# ── GET /agents/remote ────────────────────────────────────────────────────────

class TestListRemoteAgents:
    @pytest.mark.asyncio
    async def test_empty_returns_zero_count(self):
        redis = FakeRedis(decode_responses=True)
        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["agents"] == []

    @pytest.mark.asyncio
    async def test_returns_agent_with_online_status(self):
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:agent-001", _reg_entry("agent-001", age_offset=5))

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote")

        body = resp.json()
        assert body["count"] == 1
        assert body["online"] == 1
        agent = body["agents"][0]
        assert agent["agent_id"] == "agent-001"
        assert agent["status"] == "online"

    @pytest.mark.asyncio
    async def test_stale_agent_shows_offline(self):
        redis = FakeRedis(decode_responses=True)
        # last_seen 200s ago > _REMOTE_STALE_SEC=120
        await redis.set("omni:remote_agent:registry:agent-002", _reg_entry("agent-002", age_offset=200))

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote")

        body = resp.json()
        assert body["online"] == 0
        assert body["agents"][0]["status"] == "offline"

    @pytest.mark.asyncio
    async def test_embeds_metrics_when_present(self):
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:agent-001", _reg_entry("agent-001"))
        metrics = {"cpu_percent": 42.0, "mem_percent": 55.0, "disk_percent": 30.0}
        await redis.set("omni:remote_agent:metrics:agent-001", json.dumps(metrics))

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote")

        agent = resp.json()["agents"][0]
        assert agent["metrics"]["cpu_percent"] == 42.0

    @pytest.mark.asyncio
    async def test_metrics_null_when_absent(self):
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:agent-001", _reg_entry("agent-001"))

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote")

        agent = resp.json()["agents"][0]
        assert agent["metrics"] is None

    @pytest.mark.asyncio
    async def test_embeds_eps_from_zset(self):
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:agent-001", _reg_entry("agent-001"))
        now_ms = int(time.time() * 1000)
        # 30 events in last 60s
        await redis.zadd("omni:remote_agent:eps:agent-001", {f"ev-{i}": now_ms - i * 1000 for i in range(30)})

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote")

        agent = resp.json()["agents"][0]
        assert agent["eps"] == round(30 / 60.0, 4)

    @pytest.mark.asyncio
    async def test_skips_invalid_json_registry_entry(self):
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:bad", "not-json")
        await redis.set("omni:remote_agent:registry:good", _reg_entry("good"))

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote")

        body = resp.json()
        assert body["count"] == 1
        assert body["agents"][0]["agent_id"] == "good"


# ── GET /agents/remote/eps ────────────────────────────────────────────────────

class TestRemoteAgentsEps:
    @pytest.mark.asyncio
    async def test_empty_when_no_eps_keys(self):
        redis = FakeRedis(decode_responses=True)
        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote/eps")

        body = resp.json()
        assert body["total_eps"] == 0.0
        assert body["agents"] == {}

    @pytest.mark.asyncio
    async def test_returns_eps_per_agent(self):
        redis = FakeRedis(decode_responses=True)
        now_ms = int(time.time() * 1000)
        await redis.zadd("omni:remote_agent:eps:agent-A", {f"e{i}": now_ms - i * 500 for i in range(12)})
        await redis.zadd("omni:remote_agent:eps:agent-B", {f"e{i}": now_ms - i * 2000 for i in range(5)})

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote/eps")

        body = resp.json()
        assert "agent-A" in body["agents"]
        assert "agent-B" in body["agents"]
        assert body["agents"]["agent-A"] == round(12 / 60.0, 4)
        assert body["window_seconds"] == 60

    @pytest.mark.asyncio
    async def test_total_eps_sums_all_agents(self):
        redis = FakeRedis(decode_responses=True)
        now_ms = int(time.time() * 1000)
        await redis.zadd("omni:remote_agent:eps:a1", {f"e{i}": now_ms for i in range(6)})
        await redis.zadd("omni:remote_agent:eps:a2", {f"e{i}": now_ms for i in range(12)})

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote/eps")

        body = resp.json()
        assert body["total_eps"] == round(18 / 60.0, 4)


# ── GET /agents/remote/{agent_id}/logs ────────────────────────────────────────

class TestRemoteAgentLogs:
    @pytest.mark.asyncio
    async def test_empty_when_no_logs(self):
        redis = FakeRedis(decode_responses=True)
        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote/agent-001/logs")

        body = resp.json()
        assert body["agent_id"] == "agent-001"
        assert body["logs"] == []
        assert body["metrics"] is None

    @pytest.mark.asyncio
    async def test_returns_log_entries(self):
        redis = FakeRedis(decode_responses=True)
        log_entries = [{"ts": 1000.0 + i, "probe": "sys_metrics", "result": "PASSED"} for i in range(5)]
        for entry in log_entries:
            await redis.lpush("omni:remote_agent:logs:agent-001", json.dumps(entry))

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote/agent-001/logs?n=10")

        body = resp.json()
        assert len(body["logs"]) == 5
        assert body["logs"][0]["probe"] == "sys_metrics"

    @pytest.mark.asyncio
    async def test_n_parameter_limits_results(self):
        redis = FakeRedis(decode_responses=True)
        for i in range(10):
            await redis.lpush("omni:remote_agent:logs:agent-001", json.dumps({"i": i}))

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote/agent-001/logs?n=3")

        assert len(resp.json()["logs"]) == 3

    @pytest.mark.asyncio
    async def test_returns_metrics_when_present(self):
        redis = FakeRedis(decode_responses=True)
        metrics = {"cpu_percent": 75.0, "mem_percent": 60.0}
        await redis.set("omni:remote_agent:metrics:agent-001", json.dumps(metrics))

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote/agent-001/logs")

        assert resp.json()["metrics"]["cpu_percent"] == 75.0


# ── DELETE /agents/remote/{agent_id} ─────────────────────────────────────────

class TestDeregisterRemoteAgent:
    @pytest.mark.asyncio
    async def test_deregisters_and_returns_count(self):
        redis = FakeRedis(decode_responses=True)
        agent_id = "agent-001"
        await redis.set(f"omni:remote_agent:registry:{agent_id}", _reg_entry(agent_id))
        await redis.set(f"omni:remote_agent:metrics:{agent_id}", json.dumps({"cpu_percent": 10.0}))
        await redis.lpush(f"omni:remote_agent:logs:{agent_id}", json.dumps({"msg": "test"}))
        now_ms = int(time.time() * 1000)
        await redis.zadd(f"omni:remote_agent:eps:{agent_id}", {"e1": now_ms})

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete(f"/agents/remote/{agent_id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["deregistered"] == agent_id
        assert body["keys_deleted"] == 4

    @pytest.mark.asyncio
    async def test_deregister_nonexistent_returns_zero(self):
        redis = FakeRedis(decode_responses=True)
        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete("/agents/remote/ghost-agent")

        assert resp.status_code == 200
        assert resp.json()["keys_deleted"] == 0

    @pytest.mark.asyncio
    async def test_deregister_removes_from_redis(self):
        redis = FakeRedis(decode_responses=True)
        key = "omni:remote_agent:registry:agent-del"
        await redis.set(key, _reg_entry("agent-del"))

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.delete("/agents/remote/agent-del")

        assert await redis.get(key) is None


# ── Exception path coverage ──────────────────────────────────────────────────

class TestExceptionPaths:
    @pytest.mark.asyncio
    async def test_list_remote_redis_error_returns_503(self):
        from unittest.mock import AsyncMock, patch
        redis = FakeRedis(decode_responses=True)
        app = _make_app(redis)
        with patch.object(redis, "keys", side_effect=RuntimeError("redis down")):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/agents/remote")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_list_remote_skips_key_with_no_value(self):
        """Cover line 108: raw is None/empty → skip entry."""
        from unittest.mock import patch, AsyncMock
        redis = FakeRedis(decode_responses=True)

        async def fake_keys(pattern):
            return ["omni:remote_agent:registry:phantom"]

        async def fake_get(key):
            return None

        with patch.object(redis, "keys", side_effect=fake_keys):
            with patch.object(redis, "get", side_effect=fake_get):
                app = _make_app(redis)
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                    resp = await c.get("/agents/remote")
        assert resp.json()["count"] == 0

    @pytest.mark.asyncio
    async def test_eps_redis_error_returns_503(self):
        from unittest.mock import patch
        redis = FakeRedis(decode_responses=True)
        app = _make_app(redis)
        with patch.object(redis, "keys", side_effect=RuntimeError("redis down")):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/agents/remote/eps")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_logs_redis_error_returns_503(self):
        from unittest.mock import patch
        redis = FakeRedis(decode_responses=True)
        app = _make_app(redis)
        with patch.object(redis, "lrange", side_effect=RuntimeError("redis down")):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/agents/remote/agent-001/logs")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_logs_skips_invalid_json_entries(self):
        """Cover lines 193-194: JSON parse error in log entry → skip."""
        redis = FakeRedis(decode_responses=True)
        await redis.lpush("omni:remote_agent:logs:agent-001", "not-json")
        await redis.lpush("omni:remote_agent:logs:agent-001", '{"valid": true}')
        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents/remote/agent-001/logs")
        logs = resp.json()["logs"]
        assert len(logs) == 1
        assert logs[0]["valid"] is True

    @pytest.mark.asyncio
    async def test_deregister_delete_error_is_silenced(self):
        """Cover lines 225-226: delete raises → silently continue."""
        from unittest.mock import patch
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:agent-x", _reg_entry("agent-x"))
        app = _make_app(redis)
        with patch.object(redis, "delete", side_effect=RuntimeError("redis error")):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.delete("/agents/remote/agent-x")
        assert resp.status_code == 200
        assert resp.json()["keys_deleted"] == 0


# ── GET /agents (list_agents) — error path coverage ──────────────────────────

class TestListAgentsErrorPaths:
    @pytest.mark.asyncio
    async def test_skips_invalid_worker_heartbeat(self):
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:agent:heartbeat:bad-worker", "not-json")

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents")

        body = resp.json()
        assert body["workers"] == []

    @pytest.mark.asyncio
    async def test_skips_invalid_remote_registry_entry(self):
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:bad", "not-json")

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents")

        assert resp.json()["remote_agents"] == []

    @pytest.mark.asyncio
    async def test_overall_degraded_when_remote_offline(self):
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:remote_agent:registry:stale", _reg_entry("stale", age_offset=200))

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/agents")

        assert resp.json()["overall"] == "degraded"
