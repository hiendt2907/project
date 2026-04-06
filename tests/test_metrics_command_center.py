"""Command Center metrics: kill switch, Ollama probe, semaphore lanes, anomaly counter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis
from prometheus_client import REGISTRY, generate_latest

from workers import metrics_exporter as me
from workers.ollama_semaphore import RedisOllamaSemaphore
from workers.proactive_observer import evaluate_proactive_triggers
from workers.settings import WorkerSettings


@pytest_asyncio.fixture
async def fake_redis() -> FakeAsyncRedis:
    return FakeAsyncRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_sync_proactive_kill_switch_metric(fake_redis: FakeAsyncRedis) -> None:
    await me.sync_proactive_kill_switch_metric(fake_redis, "omni:proactive:kill_switch")
    out = generate_latest(REGISTRY)
    assert b"omni_proactive_kill_switch" in out
    await fake_redis.set("omni:proactive:kill_switch", "1")
    await me.sync_proactive_kill_switch_metric(fake_redis, "omni:proactive:kill_switch")
    out2 = generate_latest(REGISTRY)
    assert b"omni_proactive_kill_switch 1.0" in out2 or b"omni_proactive_kill_switch 1" in out2


@pytest.mark.asyncio
async def test_probe_ollama_up_ok() -> None:
    with patch("workers.metrics_exporter.httpx.AsyncClient") as ac:
        resp = MagicMock()
        resp.status_code = 200
        ac.return_value.__aenter__.return_value.get = AsyncMock(return_value=resp)
        await me.probe_ollama_up("http://ollama-service:11434")
    assert b"omni_ollama_up" in generate_latest(REGISTRY)


@pytest.mark.asyncio
async def test_probe_ollama_up_fail() -> None:
    with patch("workers.metrics_exporter.httpx.AsyncClient") as ac:
        ac.return_value.__aenter__.return_value.get = AsyncMock(side_effect=OSError("down"))
        await me.probe_ollama_up("http://ollama-service:11434")
    assert b"omni_ollama_up" in generate_latest(REGISTRY)


def test_semaphore_gauge_inc_dec() -> None:
    me.ollama_semaphore_inc("proactive")
    me.ollama_semaphore_inc("proactive")
    me.ollama_semaphore_dec("proactive")
    raw = generate_latest(REGISTRY).decode()
    assert "omni_ollama_semaphore_in_use" in raw
    me.ollama_semaphore_dec("proactive")


@pytest.mark.asyncio
async def test_evaluate_proactive_triggers_increments_anomaly(fake_redis: FakeAsyncRedis) -> None:
    ctx = MagicMock()
    ctx.settings = WorkerSettings()
    ctx.redis = fake_redis
    ctx.kafka = AsyncMock()
    before = generate_latest(REGISTRY).count(b"omni_anomaly_events_total")
    with patch(
        "workers.proactive_observer._instant_scalar",
        new=AsyncMock(return_value=99.0),
    ):
        n = await evaluate_proactive_triggers(ctx)
    assert n == 1
    after = generate_latest(REGISTRY)
    assert b"omni_anomaly_events_total" in after
    assert after.count(b"omni_anomaly_events_total") >= before


@pytest.mark.asyncio
async def test_semaphore_acquire_release_updates_metric(fake_redis: FakeAsyncRedis) -> None:
    sem = RedisOllamaSemaphore(fake_redis, max_slots=2, lease_ttl_sec=60)
    await sem.init_pool()
    t = await sem.acquire_proactive()
    raw = generate_latest(REGISTRY).decode()
    assert "lane=\"proactive\"" in raw or "lane=\\\"proactive\\\"" in raw
    await sem.release(t)
