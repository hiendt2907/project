"""Step 6 — POST/GET /autonomy/tier endpoint (gọi handler trực tiếp).

Bất biến: nâng tier cần confirm=true (2-step); store offline → 503; tier sai → 400.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
import pytest
from fastapi import HTTPException

from gateway.routes.autonomy import (
    TierChangeRequest,
    get_readiness,
    get_tier,
    set_tier,
)


class _Repo:
    def __init__(self, tier: str = "shadow") -> None:
        self._tier = tier
        self.set_calls: list[dict[str, Any]] = []

    async def get_tier(self, tenant_id: str = "default") -> str | None:
        return self._tier

    async def set_tier(self, **kw: Any) -> dict[str, Any]:
        self.set_calls.append(kw)
        self._tier = kw["tier"]
        return {"tier": kw["tier"], "version": 2, "dedup_key": f"tier:default:2"}


def _request(repo: Any, redis: Any = None) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(admin_repo=repo, redis=redis)))


@pytest.fixture
async def redis() -> Any:
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


async def test_get_tier():
    resp = await get_tier(_request(_Repo("assist")), tenant_id="default")
    assert json.loads(resp.body)["tier"] == "assist"


async def test_set_tier_demotion_no_confirm(redis):
    repo = _Repo("auto")
    body = TierChangeRequest(tier="shadow")  # hạ tier — không cần confirm
    resp = await set_tier(_request(repo, redis), body)
    data = json.loads(resp.body)
    assert data["status"] == "ok" and data["to"] == "shadow"
    assert repo.set_calls[0]["tier"] == "shadow"


async def test_set_tier_promotion_requires_confirm(redis):
    repo = _Repo("shadow")
    body = TierChangeRequest(tier="assist", confirm=False)
    with pytest.raises(HTTPException) as exc:
        await set_tier(_request(repo, redis), body)
    assert exc.value.status_code == 409
    assert not repo.set_calls  # không ghi


async def test_set_tier_promotion_with_confirm(redis):
    repo = _Repo("shadow")
    body = TierChangeRequest(tier="assist", confirm=True)
    resp = await set_tier(_request(repo, redis), body)
    data = json.loads(resp.body)
    assert data["promotion"] is True and data["to"] == "assist"
    assert repo.set_calls[0]["tier"] == "assist"


async def test_set_tier_invalid():
    body = TierChangeRequest(tier="god")
    with pytest.raises(HTTPException) as exc:
        await set_tier(_request(_Repo()), body)
    assert exc.value.status_code == 400


async def test_set_tier_store_offline():
    body = TierChangeRequest(tier="assist", confirm=True)
    with pytest.raises(HTTPException) as exc:
        await set_tier(_request(None), body)
    assert exc.value.status_code == 503


async def test_get_readiness_passthrough(redis):
    await redis.set("omni:tier:readiness:default", json.dumps({"ready": True, "next_tier": "assist"}))
    resp = await get_readiness(_request(_Repo(), redis), tenant_id="default")
    data = json.loads(resp.body)
    assert data["readiness"]["ready"] is True
