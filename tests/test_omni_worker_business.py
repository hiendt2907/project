"""Unit tests for omni_worker entry helpers (no full daemon loop)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_redis_str_bytes_and_none():
    from workers.omni_worker import _redis_str

    assert _redis_str(None) == ""
    assert _redis_str(b"ab\xff") == "ab\uFFFD"
    assert _redis_str(42) == "42"


@pytest.mark.asyncio
async def test_lock_heartbeat_expires_until_stop():
    import fakeredis.aioredis

    from workers.omni_worker import _lock_heartbeat

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    stop = asyncio.Event()
    task = asyncio.create_task(_lock_heartbeat(r, "lk:1", stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_telegram_fallback_callback_invalid():
    import fakeredis.aioredis

    from workers.handler_context import WorkerHandlerContext
    from workers.omni_worker import _handle_telegram_fallback_callback

    ws = SimpleNamespace(kafka_topic_alerts="omni-alerts")
    ctx = WorkerHandlerContext(
        settings=ws,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        llm=AsyncMock(),
        vector_store=MagicMock(),
        ledger=MagicMock(),
        semaphore=AsyncMock(),
        telegram=None,
        kafka=None,
    )
    assert await _handle_telegram_fallback_callback(ctx, {}) is False
    assert await _handle_telegram_fallback_callback(ctx, {"callback_query": {"data": "x"}}) is False


@pytest.mark.asyncio
async def test_telegram_fallback_callback_expired_hash():
    from workers.handler_context import WorkerHandlerContext
    from workers.omni_worker import _handle_telegram_fallback_callback

    import fakeredis.aioredis

    ws = SimpleNamespace(kafka_topic_alerts="omni-alerts")
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    tg = AsyncMock()
    ctx = WorkerHandlerContext(
        settings=ws,
        redis=r,
        llm=AsyncMock(),
        vector_store=MagicMock(),
        ledger=MagicMock(),
        semaphore=AsyncMock(),
        telegram=tg,
        kafka=MagicMock(),
    )
    u = {"callback_query": {"id": "cq1", "data": "ofs:missinghash:0"}, "update_id": 1}
    assert await _handle_telegram_fallback_callback(ctx, u) is True
    tg.answer_callback_query.assert_awaited()
