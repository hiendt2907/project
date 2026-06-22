"""Coverage: AutonomyGate.get_fp_rate_for_lane (evaluate requires CRAT-capable redis)."""

from __future__ import annotations

import pytest

from pkg.autonomy.gate import AutonomyGate


@pytest.mark.asyncio
async def test_get_fp_rate_for_lane_none_redis() -> None:
    gate = AutonomyGate()
    assert await gate.get_fp_rate_for_lane("SYS_RESOURCE", None) == 0.0


@pytest.mark.asyncio
async def test_get_fp_rate_for_lane_ratio() -> None:
    gate = AutonomyGate()

    class ZRedis:
        async def zcount(self, _key: str, *_a: object, **_k: object) -> int:
            if "false_positive" in _key:
                return 1
            return 3

    rate = await gate.get_fp_rate_for_lane("any", ZRedis())
    assert abs(rate - 0.25) < 1e-9


@pytest.mark.asyncio
async def test_get_fp_rate_for_lane_zcount_error_returns_zero() -> None:
    gate = AutonomyGate()

    class BadRedis:
        async def zcount(self, *_a: object, **_k: object) -> int:
            raise OSError("simulated")

    assert await gate.get_fp_rate_for_lane("lane", BadRedis()) == 0.0
