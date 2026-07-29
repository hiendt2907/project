"""G2 — nâng tier còn cần bằng chứng có QUY TRÌNH LẶP LẠI ĐƯỢC, không chỉ acceptance cao.

`compute_tier_readiness` vốn đã đủ tốt (Wilson LB, elapsed_days, fp_rate, key per-tenant).
Thiếu duy nhất một điều kiện: operator chấp nhận nhiều advisory KHÔNG chứng minh Omni đã
rút ra được quy trình nào. Đó chính là thứ G1 đo bằng `omni_admin.playbook_graduation`.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import fakeredis.aioredis
import pytest

from workers.tier_readiness import compute_tier_readiness


def _settings():
    return SimpleNamespace(
        omni_tier_min_advisories=10,
        omni_tier_max_false_positive_rate=0.10,
        omni_tier_min_graduated_playbooks=1,
    )


async def _seed(redis, tenant="default", accepted=20):
    now = time.time()
    for i in range(accepted):
        await redis.zadd(f"omni:kpi:z:{tenant}:accepted", {f"a{i}": now})


@pytest.mark.asyncio
async def test_blocked_when_no_graduated_playbook():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await _seed(redis)

    r = await compute_tier_readiness(
        redis=redis, settings=_settings(), current_tier="shadow",
        tier_entered_at=time.time() - 86400 * 60, graduated_playbooks=0,
    )

    assert r.ready is False
    assert any("playbook" in x.lower() for x in r.reasons)


@pytest.mark.asyncio
async def test_ready_when_graduated_playbook_present():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await _seed(redis)

    r = await compute_tier_readiness(
        redis=redis, settings=_settings(), current_tier="shadow",
        tier_entered_at=time.time() - 86400 * 120, graduated_playbooks=2,
    )

    assert r.ready is True
    assert r.graduated_playbooks == 2


@pytest.mark.asyncio
async def test_graduation_gate_cannot_override_other_failures():
    """Có playbook tốt nghiệp KHÔNG bù được cho việc thiếu mẫu."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await _seed(redis, accepted=2)  # dưới min_advisories

    r = await compute_tier_readiness(
        redis=redis, settings=_settings(), current_tier="shadow",
        tier_entered_at=time.time() - 86400 * 60, graduated_playbooks=99,
    )

    assert r.ready is False


@pytest.mark.asyncio
async def test_default_zero_graduations_is_backward_compatible_block():
    """Caller cũ không truyền tham số → coi như 0, và bị chặn (fail-closed)."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await _seed(redis)

    r = await compute_tier_readiness(
        redis=redis, settings=_settings(), current_tier="shadow",
        tier_entered_at=time.time() - 86400 * 60,
    )

    assert r.ready is False
