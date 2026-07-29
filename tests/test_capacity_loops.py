"""G4 wiring — loop capacity/report chỉ đọc và publish, không mutate gì."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import fakeredis.aioredis
import pytest

from workers.capacity_loops import (
    _parse_baseline_key,
    capacity_report_loop,
    collect_capacity_advice,
)


def _redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def _seed(redis, tenant="acme", host="db-1", metric="cpu", value=5.0, n=60):
    key = f"3sigma:remote:{tenant}:{host}:{metric}"
    for _ in range(n):
        await redis.rpush(key, value)


def test_parse_key_extracts_identity():
    assert _parse_baseline_key("3sigma:remote:acme:db-1:cpu") == ("acme", "db-1", "cpu")


def test_parse_key_rejects_foreign_shapes():
    assert _parse_baseline_key("omni:kpi:z:acme:accepted") is None
    assert _parse_baseline_key("3sigma:remote:acme") is None
    assert _parse_baseline_key("3sigma:remote:acme::cpu") is None


@pytest.mark.asyncio
async def test_collect_groups_by_tenant():
    r = _redis()
    await _seed(r, tenant="acme")
    await _seed(r, tenant="globex", host="app-1", metric="mem", value=50.0)

    out = await collect_capacity_advice(r)

    assert set(out) == {"acme", "globex"}
    assert out["acme"][0].host == "db-1"


@pytest.mark.asyncio
async def test_collect_survives_one_bad_key():
    r = _redis()
    await _seed(r, tenant="acme")
    await r.set("3sigma:remote:acme:bad:cpu", "not-a-list")  # lrange sẽ lỗi kiểu

    out = await collect_capacity_advice(r)

    assert "acme" in out


@pytest.mark.asyncio
async def test_loop_publishes_advice_and_report_then_stops():
    r = _redis()
    await _seed(r, tenant="acme")
    ctx = SimpleNamespace(
        redis=r, admin_pool=None,
        settings=SimpleNamespace(omni_capacity_advisor_enabled=True),
    )
    stop = asyncio.Event()

    task = asyncio.create_task(capacity_report_loop(ctx, stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    advice = json.loads(await r.get("omni:capacity:advice:acme"))
    report = await r.get("omni:report:sre:acme")

    assert advice and advice[0]["tenant_id"] == "acme"
    assert report.startswith("# Báo cáo")


@pytest.mark.asyncio
async def test_loop_respects_disable_flag():
    r = _redis()
    await _seed(r, tenant="acme")
    ctx = SimpleNamespace(
        redis=r, admin_pool=None,
        settings=SimpleNamespace(omni_capacity_advisor_enabled=False),
    )

    await asyncio.wait_for(capacity_report_loop(ctx, asyncio.Event()), timeout=5)

    assert await r.get("omni:capacity:advice:acme") is None


@pytest.mark.asyncio
async def test_published_advice_never_carries_an_executable_action():
    """Chốt an toàn: portal đọc được đề xuất nhưng không có tool/args để chạy."""
    r = _redis()
    await _seed(r, tenant="acme")
    ctx = SimpleNamespace(
        redis=r, admin_pool=None,
        settings=SimpleNamespace(omni_capacity_advisor_enabled=True),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(capacity_report_loop(ctx, stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    advice = json.loads(await r.get("omni:capacity:advice:acme"))

    assert "tool" not in advice[0]
    assert "args" not in advice[0]
    assert advice[0]["auto_execute"] is False
