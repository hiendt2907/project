"""Deep Scout: redaction, embedding concurrency, periodic cancel."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from init.deep_scout import (
    DeepScoutSummary,
    _embed_and_upsert,
    _is_sensitive_config_key,
    _redact_configmap_entries,
    deep_scout_periodic_loop,
    run_deep_scout,
)


def test_sensitive_key_detects_api_key() -> None:
    assert _is_sensitive_config_key("MY_API_KEY")
    assert _is_sensitive_config_key("password_hash")
    assert not _is_sensitive_config_key("server_port")


def test_redact_configmap_entries() -> None:
    raw = {"api_key": "supersecret", "PORT": "6379", "user_password": "x"}
    out = _redact_configmap_entries(raw)
    assert out["api_key"] == "<REDACTED>"
    assert out["user_password"] == "<REDACTED>"
    assert out["PORT"] == "6379"


@pytest.mark.asyncio
async def test_embed_semaphore_limits_concurrency() -> None:
    concurrent = 0
    max_c = 0
    lock = asyncio.Lock()

    ollama = MagicMock()

    async def fake_embed(**kwargs: object) -> dict:
        nonlocal concurrent, max_c
        async with lock:
            concurrent += 1
            max_c = max(max_c, concurrent)
        await asyncio.sleep(0.05)
        async with lock:
            concurrent -= 1
        return {"embedding": [0.01] * 768}

    ollama.embed = AsyncMock(side_effect=fake_embed)
    ws = MagicMock()
    ws.embed_model = "m"
    ws.ollama_keep_alive = "5m"
    ws.deep_scout_embed_concurrency = 3
    vector_store = MagicMock()
    vector_store.upsert = AsyncMock()
    sem = asyncio.Semaphore(ws.deep_scout_embed_concurrency)
    chunks = [
        (f"id{i}", f"text{i}", {"kind": "t"}) for i in range(12)
    ]
    await _embed_and_upsert(ollama, ws, vector_store, chunks, sem)
    assert max_c <= 3


@pytest.mark.asyncio
async def test_deep_scout_periodic_cancels_on_stop() -> None:
    stop = asyncio.Event()
    ctx = MagicMock()
    ctx.settings.deep_scout_interval_sec = 3600
    t = asyncio.create_task(deep_scout_periodic_loop(ctx, stop))
    stop.set()
    await asyncio.wait_for(t, timeout=2.0)


@pytest.mark.asyncio
async def test_run_deep_scout_sets_redis_keys_host_baseline_only() -> None:
    r = AsyncMock()
    r.set = AsyncMock()
    ollama = MagicMock()
    ollama.embed = AsyncMock(return_value={"embedding": [0.02] * 768})
    vector_store = MagicMock()
    vector_store.upsert = AsyncMock()
    ws = MagicMock()
    ws.prometheus_url = "http://prometheus:9090"
    ws.deep_scout_embed_concurrency = 2
    ws.deep_scout_configmap_namespaces = "default"
    ws.embed_model = "m"
    ws.ollama_keep_alive = "5m"
    ctx = MagicMock()
    ctx.redis = r
    ctx.ollama = ollama
    ctx.vector_store = vector_store
    ctx.settings = ws

    with patch("init.deep_scout._layer_host_node", new_callable=AsyncMock, return_value=({}, "h")):
        with patch("init.deep_scout._layer_network_topology", new_callable=AsyncMock, return_value=({"services": []}, "t")):
            with patch("init.deep_scout._layer_metrics_baseline", new_callable=AsyncMock, return_value=({}, "m")):
                with patch("init.deep_scout._layer_cluster_state", new_callable=AsyncMock, return_value=({"pod_count": 0}, "c")):
                    s = await run_deep_scout(ctx, periodic=False)
    assert isinstance(s, DeepScoutSummary)
    # Topology no longer persisted to Redis (pgvector only).
    assert r.set.await_count >= 2
