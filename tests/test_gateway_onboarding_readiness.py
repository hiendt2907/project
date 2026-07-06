"""Iteration 26 (ADR-003 parity): GET /onboarding/readiness trả thêm thresholds
để portal hiển thị "X% so với mục tiêu Y%" cho người không hiểu hệ thống."""
from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pkg.onboarding.discovery_doc import DEFAULT_READINESS_THRESHOLDS
from gateway.routes.onboarding import router


class StubAdminRepo:
    def __init__(self) -> None:
        self.readiness: dict[str, dict[str, Any]] = {}
        self.flags: dict[tuple[str, str], Any] = {}

    async def get_tenant_readiness(self, tenant_id: str = "default") -> dict[str, Any] | None:
        return self.readiness.get(tenant_id)

    async def get_runtime_flag(self, flag_key: str, tenant_id: str = "default") -> Any | None:
        return self.flags.get((tenant_id, flag_key))


def _app(repo: StubAdminRepo) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.state.admin_repo = repo
    return app


class TestReadinessEndpointThresholds:
    @pytest.mark.asyncio
    async def test_readiness_includes_default_thresholds(self):
        repo = StubAdminRepo()
        repo.readiness["acme"] = {
            "endpoint_mapped_pct": 66.7,
            "business_flow_confirmed_pct": 0.0,
            "open_questions_over_threshold": 2,
            "readiness_flag": False,
            "updated_at": "2026-07-06T00:00:00",
        }
        async with AsyncClient(transport=ASGITransport(app=_app(repo)), base_url="http://test") as c:
            resp = await c.get("/onboarding/readiness", params={"tenant_id": "acme"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["readiness"]["endpoint_mapped_pct"] == 66.7
        assert data["thresholds"] == DEFAULT_READINESS_THRESHOLDS

    @pytest.mark.asyncio
    async def test_readiness_thresholds_respect_tenant_override(self):
        repo = StubAdminRepo()
        repo.flags[("acme", "readiness_threshold:acme")] = {"endpoint_mapped_pct_min": 50.0}
        async with AsyncClient(transport=ASGITransport(app=_app(repo)), base_url="http://test") as c:
            resp = await c.get("/onboarding/readiness", params={"tenant_id": "acme"})
        assert resp.status_code == 200
        data = resp.json()
        # readiness record absent → null, nhưng thresholds vẫn phải có để UI vẽ target
        assert data["readiness"] is None
        assert data["thresholds"]["endpoint_mapped_pct_min"] == 50.0
        assert data["thresholds"]["open_questions_max"] == DEFAULT_READINESS_THRESHOLDS["open_questions_max"]
