"""Unit tests for src/gateway/routes/kpi.py — clusters, prompt-ab, trend error paths."""
from __future__ import annotations

import time

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _make_app(redis=None) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis
    from gateway.routes.kpi import router
    app.include_router(router)
    return app


# ── /kpi/summary ─────────────────────────────────────────────────────────────

class TestKpiSummary:
    @pytest.mark.asyncio
    async def test_returns_zero_when_no_data(self):
        redis = FakeRedis(decode_responses=True)
        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["advisory"]["accepted"] == 0
        assert body["advisory"]["acceptance_rate"] is None

    @pytest.mark.asyncio
    async def test_calculates_acceptance_rate(self):
        redis = FakeRedis(decode_responses=True)
        now = time.time()
        for i in range(7):
            await redis.zadd("omni:kpi:z:default:accepted", {f"a{i}": now - i})
        for i in range(3):
            await redis.zadd("omni:kpi:z:default:rejected", {f"r{i}": now - i})

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/summary")

        body = resp.json()
        assert body["advisory"]["accepted"] == 7
        assert body["advisory"]["rejected"] == 3
        assert body["advisory"]["acceptance_rate"] == 0.7

    @pytest.mark.asyncio
    async def test_redis_none_returns_503(self):
        app = _make_app(redis=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/summary")
        assert resp.status_code == 503


# ── /kpi/trend ────────────────────────────────────────────────────────────────

class TestKpiTrend:
    @pytest.mark.asyncio
    async def test_returns_all_domains(self):
        """Trend nhóm theo 9 domain canonical + `unknown` (đổi 2026-07-30)."""
        redis = FakeRedis(decode_responses=True)
        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/trend?window=1h")

        body = resp.json()
        for d in ("os_host", "application", "security", "network", "storage",
                  "database", "service", "kubernetes", "hardware", "unknown"):
            assert d in body["domains"], f"thieu domain {d}"
        assert body["window_seconds"] == 3600

    @pytest.mark.asyncio
    async def test_lanes_key_is_backward_compat_alias(self):
        """UI/E2E cu doc `lanes` — phai tra ve cung du lieu voi `domains`."""
        redis = FakeRedis(decode_responses=True)
        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/trend?window=1h")
        body = resp.json()
        assert body["lanes"] == body["domains"]

    @pytest.mark.asyncio
    async def test_domain_counts_from_redis(self):
        redis = FakeRedis(decode_responses=True)
        now = time.time()
        for i in range(5):
            await redis.zadd("omni:kpi:detected:default:os_host", {f"d{i}": now - i * 60})
        for i in range(3):
            await redis.zadd("omni:kpi:resolved:default:os_host", {f"r{i}": now - i * 60})

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/trend?window=6h")

        bucket = resp.json()["domains"]["os_host"]
        assert bucket["detected"] == 5
        assert bucket["resolved"] == 3

    @pytest.mark.asyncio
    async def test_trend_exception_path_returns_zeros(self):
        """Cover lines 68-69: exception in zcount falls back to 0."""
        from unittest.mock import AsyncMock, patch
        redis = FakeRedis(decode_responses=True)
        app = _make_app(redis)

        # Patch zcount to raise on the security domain only
        original_zcount = redis.zcount
        call_count = 0

        async def patched_zcount(key, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if ":security" in key:
                raise RuntimeError("simulated redis error")
            return await original_zcount(key, *args, **kwargs)

        with patch.object(redis, "zcount", side_effect=patched_zcount):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/kpi/trend?window=1h")

        body = resp.json()
        # security domain should fall back to 0 not raise
        assert body["domains"]["security"]["detected"] == 0
        assert body["domains"]["security"]["resolved"] == 0


# ── /kpi/clusters ─────────────────────────────────────────────────────────────

class TestKpiClusters:
    @pytest.mark.asyncio
    async def test_empty_when_no_clusters(self):
        redis = FakeRedis(decode_responses=True)
        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/clusters")

        body = resp.json()
        assert body["total"] == 0
        assert body["clusters"] == []

    @pytest.mark.asyncio
    async def test_returns_cluster_list(self):
        redis = FakeRedis(decode_responses=True)
        await redis.hset("omni:cluster:cluster-001", mapping={
            "cluster_id": "cluster-001",
            "namespace": "multi-agent",
            "member_count": "5",
            "created_at": "1700000000.0",
        })
        await redis.hset("omni:cluster:cluster-002", mapping={
            "cluster_id": "cluster-002",
            "namespace": "default",
            "member_count": "2",
            "created_at": "1700000100.0",
        })

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/clusters")

        body = resp.json()
        assert body["total"] == 2
        # Sorted descending by member_count
        assert body["clusters"][0]["member_count"] == 5
        assert body["clusters"][0]["namespace"] == "multi-agent"

    @pytest.mark.asyncio
    async def test_skips_meta_keys(self):
        redis = FakeRedis(decode_responses=True)
        await redis.hset("omni:cluster:cluster-001", mapping={"cluster_id": "c1", "namespace": "ns", "member_count": "1", "created_at": "0"})
        await redis.hset("omni:cluster:meta:stats", mapping={"total": "10"})

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/clusters")

        body = resp.json()
        # meta: key must be skipped
        assert body["total"] == 1

    @pytest.mark.asyncio
    async def test_redis_none_returns_503(self):
        app = _make_app(redis=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/clusters")
        assert resp.status_code == 503


# ── /kpi/prompt-ab ────────────────────────────────────────────────────────────

class TestKpiPromptAb:
    @pytest.mark.asyncio
    async def test_empty_variants_when_no_data(self):
        redis = FakeRedis(decode_responses=True)
        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/prompt-ab")

        body = resp.json()
        assert body["variants"]["A"] == {}
        assert body["variants"]["B"] == {}
        assert body["winner"] is None

    @pytest.mark.asyncio
    async def test_returns_variant_stats(self):
        redis = FakeRedis(decode_responses=True)
        await redis.hset("omni:prompt:ab:A", mapping={
            "total": "10",
            "json_ok": "8",
            "steps_sum": "30",
            "success": "7",
        })
        await redis.hset("omni:prompt:ab:B", mapping={
            "total": "10",
            "json_ok": "9",
            "steps_sum": "25",
            "success": "9",
        })
        await redis.set("omni:prompt:ab:winner", "B")
        await redis.set("omni:prompt:ab:winner_at", "1700000000")

        app = _make_app(redis)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/prompt-ab")

        body = resp.json()
        assert body["variants"]["A"]["total"] == 10
        assert body["variants"]["A"]["json_ok_rate"] == 0.8
        assert body["variants"]["A"]["avg_steps"] == 3.0
        assert body["variants"]["B"]["success_rate"] == 0.9
        assert body["winner"] == "B"
        assert body["winner_at"] == "1700000000"

    @pytest.mark.asyncio
    async def test_redis_none_returns_503(self):
        app = _make_app(redis=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kpi/prompt-ab")
        assert resp.status_code == 503
