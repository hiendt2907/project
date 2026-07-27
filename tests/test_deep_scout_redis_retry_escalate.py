"""TDD: deep_scout Redis write retry + escalate-to-ErrorLedger (thay vì nuốt lỗi
im lặng sau 1 lần fail — Phase 2, bug omni-core)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from init.deep_scout import _retry_redis_write, run_deep_scout


@pytest.mark.asyncio
class TestRetryRedisWrite:
    async def test_succeeds_first_try_no_retry(self):
        calls = []

        async def ok():
            calls.append(1)

        await _retry_redis_write(ok, max_attempts=3)
        assert len(calls) == 1

    async def test_succeeds_after_transient_failure(self):
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise TimeoutError("redis timeout")

        await _retry_redis_write(flaky, max_attempts=3)
        assert len(calls) == 2

    async def test_raises_after_exhausting_attempts(self):
        calls = []

        async def always_fails():
            calls.append(1)
            raise TimeoutError("redis timeout")

        with pytest.raises(TimeoutError):
            await _retry_redis_write(always_fails, max_attempts=3)
        assert len(calls) == 3


@pytest.mark.asyncio
async def test_run_deep_scout_escalates_to_error_ledger_after_retry_exhausted(monkeypatch):
    async def fake_layer_host(ws):
        return {"nodes": []}, ""

    async def fake_layer_topo():
        return {"services": []}, ""

    async def fake_layer_met(ws):
        return {"queries": {}}, ""

    async def fake_layer_cluster(ws):
        return {"pod_count": 0}, ""

    async def fake_embed_upsert(llm, ws, vs, chunks, sem):
        pass

    monkeypatch.setattr("init.deep_scout._layer_host_node", fake_layer_host)
    monkeypatch.setattr("init.deep_scout._layer_network_topology", fake_layer_topo)
    monkeypatch.setattr("init.deep_scout._layer_metrics_baseline", fake_layer_met)
    monkeypatch.setattr("init.deep_scout._layer_cluster_state", fake_layer_cluster)
    monkeypatch.setattr("init.deep_scout._embed_and_upsert", fake_embed_upsert)
    monkeypatch.setattr("init.deep_scout._REDIS_WRITE_BACKOFF_SEC", 0.0)

    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(side_effect=TimeoutError("redis timeout"))
    fake_ledger = AsyncMock()

    ws = SimpleNamespace(deep_scout_embed_concurrency=2, prometheus_url="http://prom:9090")
    ctx = SimpleNamespace(
        settings=ws, redis=fake_redis, llm=AsyncMock(), vector_store=AsyncMock(), ledger=fake_ledger,
    )

    summary = await run_deep_scout(ctx)

    assert any("redis" in e for e in summary.errors)
    fake_ledger.record_exception.assert_awaited_once()
    _, kwargs = fake_ledger.record_exception.await_args
    assert kwargs["component"] == "deep_scout_redis_write"
    assert kwargs["swallow_errors"] is True


@pytest.mark.asyncio
async def test_run_deep_scout_missing_ledger_does_not_raise(monkeypatch):
    """ctx sans .ledger (old call sites) — không crash khi escalate."""

    async def fake_layer_host(ws):
        return {"nodes": []}, ""

    async def fake_layer_topo():
        return {"services": []}, ""

    async def fake_layer_met(ws):
        return {"queries": {}}, ""

    async def fake_layer_cluster(ws):
        return {"pod_count": 0}, ""

    async def fake_embed_upsert(llm, ws, vs, chunks, sem):
        pass

    monkeypatch.setattr("init.deep_scout._layer_host_node", fake_layer_host)
    monkeypatch.setattr("init.deep_scout._layer_network_topology", fake_layer_topo)
    monkeypatch.setattr("init.deep_scout._layer_metrics_baseline", fake_layer_met)
    monkeypatch.setattr("init.deep_scout._layer_cluster_state", fake_layer_cluster)
    monkeypatch.setattr("init.deep_scout._embed_and_upsert", fake_embed_upsert)
    monkeypatch.setattr("init.deep_scout._REDIS_WRITE_BACKOFF_SEC", 0.0)

    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(side_effect=TimeoutError("redis timeout"))

    ws = SimpleNamespace(deep_scout_embed_concurrency=2, prometheus_url="http://prom:9090")
    ctx = SimpleNamespace(settings=ws, redis=fake_redis, llm=AsyncMock(), vector_store=AsyncMock())

    summary = await run_deep_scout(ctx)
    assert any("redis" in e for e in summary.errors)
