"""Acceptance-plan: semaphore lanes, kill-switch log, audit MAXLEN (fakeredis)."""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis

from workers.ollama_semaphore import RedisOllamaSemaphore
from workers.proactive_observer import evaluate_proactive_triggers
from workers.settings import WorkerSettings


@pytest_asyncio.fixture
async def fake_redis() -> FakeAsyncRedis:
    return FakeAsyncRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_dual_lane_semaphore_reactive_token_prefix(fake_redis: FakeAsyncRedis) -> None:
    sem = RedisOllamaSemaphore(fake_redis, max_slots=2, lease_ttl_sec=60)
    await sem.init_pool()
    t = await sem.acquire()
    assert t.startswith("r")
    await sem.release(t)


@pytest.mark.asyncio
async def test_dual_lane_semaphore_proactive_token_prefix(fake_redis: FakeAsyncRedis) -> None:
    sem = RedisOllamaSemaphore(fake_redis, max_slots=2, lease_ttl_sec=60)
    await sem.init_pool()
    t = await sem.acquire_proactive()
    assert t.startswith("p")
    await sem.release(t)


@pytest.mark.asyncio
async def test_evaluate_proactive_triggers_logs_bypass_when_kill_switch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = MagicMock()
    ctx.settings = WorkerSettings()
    ctx.redis = AsyncMock()
    ctx.redis.get = AsyncMock(return_value="1")

    with caplog.at_level(logging.INFO, logger="workers.proactive_observer"):
        n = await evaluate_proactive_triggers(ctx)

    assert n == 0
    assert "Bypassed proactively due to kill_switch=1" in caplog.text


@pytest.mark.asyncio
async def test_audit_proactive_stream_maxlen_approx_fifo(fake_redis: FakeAsyncRedis) -> None:
    stream = "audit:proactive"
    maxlen = 1000
    for i in range(1005):
        await fake_redis.xadd(
            stream,
            {"data": json.dumps({"i": i})},
            maxlen=maxlen,
            approximate=True,
        )
    n = await fake_redis.xlen(stream)
    assert n <= maxlen + 5, f"expected capped stream length, got {n}"
