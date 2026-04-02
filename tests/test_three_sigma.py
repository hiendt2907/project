"""Prove 3-sigma gate: TTL on every write, bounded keys (no unbounded RAM growth per metric)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis

from anomaly.three_sigma import ThreeSigmaGate


@pytest_asyncio.fixture
async def fake_redis() -> FakeAsyncRedis:
    return FakeAsyncRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_pipeline_sets_ttl_after_observe(fake_redis: FakeAsyncRedis) -> None:
    gate = ThreeSigmaGate(fake_redis, window_size=10, ttl_sec=3600)
    for i in range(3):
        await gate.observe("cpu.load", 1.0 + i * 0.01)
    ttl = await gate.ttl_for("cpu.load")
    assert ttl > 0, "EXPIRE must refresh key lifetime (no orphan keys without TTL)"


@pytest.mark.asyncio
async def test_single_metric_one_key_no_leak(fake_redis: FakeAsyncRedis) -> None:
    gate = ThreeSigmaGate(fake_redis, window_size=50, ttl_sec=3600)
    for i in range(2000):
        await gate.observe("latency_ms", float(i % 17))
    assert await gate.key_count_estimate() == 1


@pytest.mark.asyncio
async def test_many_distinct_metrics_bounded_keys(fake_redis: FakeAsyncRedis) -> None:
    gate = ThreeSigmaGate(fake_redis, window_size=20, ttl_sec=60)
    for i in range(100):
        await gate.observe(f"host{i % 10}.metric", float(i))
    assert await gate.key_count_estimate() == 10


@pytest.mark.asyncio
async def test_anomaly_when_z_exceeds_three(fake_redis: FakeAsyncRedis) -> None:
    gate = ThreeSigmaGate(fake_redis, window_size=30, ttl_sec=120)
    for _ in range(29):
        await gate.observe("stable", 1.0)
    is_a, z = await gate.observe("stable", 100.0)
    assert is_a is True
    assert z is not None and abs(z) > 3.0


@pytest.mark.asyncio
async def test_not_enough_samples_not_anomaly(fake_redis: FakeAsyncRedis) -> None:
    gate = ThreeSigmaGate(fake_redis, window_size=100, ttl_sec=3600)
    await gate.observe("new", 5.0)
    is_a, z = await gate.observe("new", 5.0)
    assert is_a is False
    assert z is None
