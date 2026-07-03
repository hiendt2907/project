"""Iteration 23: GET /onboarding/diagram/history — anchored at latest, paginated backwards.

The old contract (from_version/to_version, capped at 200) could never reach recent
versions once latest passed 200 (lab tenant staging-sim is at v10022+). The new
contract walks DOWN from `before` (default: latest) and returns up to `limit`
versions, newest-first.
"""
from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from gateway.routes.onboarding import router
from pkg.onboarding import discovery_doc as dd


def _app(redis: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.redis = redis
    return app


def _redis() -> Any:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def _seed_versions(redis: Any, tenant_id: str, count: int) -> None:
    for v in range(1, count + 1):
        await redis.set(
            dd.DIAGRAM_KEY.format(tenant_id=tenant_id, version=v),
            f"%% component\ngraph TD\n  A{v} --> B{v}\n",
        )
    await redis.set(dd.DIAGRAM_LATEST_KEY.format(tenant_id=tenant_id), count)


class TestDiagramHistory:
    @pytest.mark.asyncio
    async def test_empty_tenant_returns_no_versions(self):
        r = _redis()
        async with AsyncClient(transport=ASGITransport(app=_app(r)), base_url="http://test") as c:
            resp = await c.get("/onboarding/diagram/history", params={"tenant_id": "acme"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["latest"] is None
        assert data["versions"] == []
        assert data["next_before"] is None

    @pytest.mark.asyncio
    async def test_returns_newest_first_anchored_at_latest(self):
        r = _redis()
        await _seed_versions(r, "acme", 5)
        async with AsyncClient(transport=ASGITransport(app=_app(r)), base_url="http://test") as c:
            resp = await c.get("/onboarding/diagram/history", params={"tenant_id": "acme", "limit": 3})
        assert resp.status_code == 200
        data = resp.json()
        assert data["latest"] == 5
        assert [v["version"] for v in data["versions"]] == [5, 4, 3]
        assert "A5" in data["versions"][0]["mermaid"]
        assert data["next_before"] == 3  # older pages exist

    @pytest.mark.asyncio
    async def test_before_paginates_older_versions(self):
        r = _redis()
        await _seed_versions(r, "acme", 5)
        async with AsyncClient(transport=ASGITransport(app=_app(r)), base_url="http://test") as c:
            resp = await c.get(
                "/onboarding/diagram/history",
                params={"tenant_id": "acme", "before": 3, "limit": 10},
            )
        data = resp.json()
        assert [v["version"] for v in data["versions"]] == [2, 1]
        assert data["next_before"] is None  # reached version 1

    @pytest.mark.asyncio
    async def test_missing_versions_are_skipped(self):
        r = _redis()
        await _seed_versions(r, "acme", 4)
        await r.delete(dd.DIAGRAM_KEY.format(tenant_id="acme", version=3))
        async with AsyncClient(transport=ASGITransport(app=_app(r)), base_url="http://test") as c:
            resp = await c.get("/onboarding/diagram/history", params={"tenant_id": "acme", "limit": 3})
        data = resp.json()
        assert [v["version"] for v in data["versions"]] == [4, 2, 1]

    @pytest.mark.asyncio
    async def test_limit_bounds_enforced(self):
        r = _redis()
        async with AsyncClient(transport=ASGITransport(app=_app(r)), base_url="http://test") as c:
            resp_high = await c.get("/onboarding/diagram/history", params={"tenant_id": "acme", "limit": 51})
            resp_zero = await c.get("/onboarding/diagram/history", params={"tenant_id": "acme", "limit": 0})
        assert resp_high.status_code == 422
        assert resp_zero.status_code == 422
