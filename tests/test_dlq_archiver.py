"""Unit tests for DLQ archiver — archiving + time-window rollup alerting (plan step 2)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from workers.dlq_archiver import (
    _REDIS_KEY,
    _accumulate_rollup,
    _bucket_id,
    _handle_dlq_message,
    flush_completed_buckets,
)


def _make_ctx(redis=None, telegram=None, settings=None):  # type: ignore[no-untyped-def]
    fake_redis = redis or fakeredis.aioredis.FakeRedis(decode_responses=True)
    ws = settings or SimpleNamespace(
        kafka_bootstrap_servers="kafka:9092",
        kafka_topic_dlq="omni-dlq",
        telegram_admin_chat_id=None,
        dlq_rollup_window_sec=60,
    )
    ctx = MagicMock()
    ctx.redis = fake_redis
    ctx.settings = ws
    ctx.telegram = telegram
    return ctx


class TestHandleDlqMessage:
    async def test_valid_payload_archived_to_redis(self) -> None:
        ctx = _make_ctx()
        payload = {"trace_id": "abc-123", "origin_topic": "omni-evidence", "error": "timeout"}
        raw = json.dumps(payload).encode()

        await _handle_dlq_message(ctx, raw)

        members = await ctx.redis.zrange(_REDIS_KEY, 0, -1)
        assert len(members) == 1

    async def test_invalid_json_handled_gracefully(self) -> None:
        ctx = _make_ctx()
        raw = b"not-json-at-all"

        await _handle_dlq_message(ctx, raw)

        members = await ctx.redis.zrange(_REDIS_KEY, 0, -1)
        assert len(members) == 1

    async def test_handle_accumulates_rollup_bucket(self) -> None:
        ctx = _make_ctx()
        for i in range(3):
            await _handle_dlq_message(
                ctx, json.dumps({"trace_id": f"t{i}", "error": "timeout"}).encode()
            )
        # The current bucket total should reflect all 3 failures.
        import time as _t

        bucket = _bucket_id(_t.time(), 60)
        total = await ctx.redis.get(f"omni:dlq:rollup:{bucket}:total")
        assert int(total) == 3


class TestRollup:
    async def test_accumulate_is_atomic_and_groups_by_reason(self) -> None:
        ctx = _make_ctx()
        ts = 1_000_000.0
        for _ in range(5):
            await _accumulate_rollup(ctx, "connection refused", ts)
        for _ in range(2):
            await _accumulate_rollup(ctx, "timeout", ts)

        bucket = _bucket_id(ts, 60)
        total = await ctx.redis.get(f"omni:dlq:rollup:{bucket}:total")
        assert int(total) == 7
        top = await ctx.redis.zrevrange(
            f"omni:dlq:rollup:{bucket}:reasons", 0, -1, withscores=True
        )
        # connection refused (5) dominates timeout (2).
        assert top[0][0] in ("connection refused",)
        assert int(top[0][1]) == 5

    async def test_flush_emits_single_rollup_for_completed_bucket(self) -> None:
        telegram = AsyncMock()
        ws = SimpleNamespace(
            kafka_bootstrap_servers="kafka:9092",
            kafka_topic_dlq="omni-dlq",
            telegram_admin_chat_id=42,
            dlq_rollup_window_sec=60,
        )
        ctx = _make_ctx(telegram=telegram, settings=ws)
        ts = 1_000_000.0
        for _ in range(10):
            await _accumulate_rollup(ctx, "boom", ts)

        # now is in the NEXT window → the ts bucket is completed and flushable.
        flushed = await flush_completed_buckets(ctx, now=ts + 120.0)

        assert flushed == 1
        telegram.send_message.assert_called_once()
        body = telegram.send_message.call_args[0][1]
        assert "10" in body and "boom" in body

    async def test_flush_is_idempotent_single_winner(self) -> None:
        telegram = AsyncMock()
        ws = SimpleNamespace(
            kafka_bootstrap_servers="kafka:9092",
            kafka_topic_dlq="omni-dlq",
            telegram_admin_chat_id=7,
            dlq_rollup_window_sec=60,
        )
        ctx = _make_ctx(telegram=telegram, settings=ws)
        ts = 2_000_000.0
        await _accumulate_rollup(ctx, "x", ts)

        first = await flush_completed_buckets(ctx, now=ts + 120.0)
        second = await flush_completed_buckets(ctx, now=ts + 120.0)

        assert first == 1
        assert second == 0  # claimed already → no duplicate notification
        telegram.send_message.assert_called_once()

    async def test_current_bucket_not_flushed_yet(self) -> None:
        ctx = _make_ctx()
        ts = 3_000_000.0
        await _accumulate_rollup(ctx, "y", ts)
        # now still in the SAME window → bucket not complete.
        flushed = await flush_completed_buckets(ctx, now=ts + 1.0)
        assert flushed == 0


class TestDlqMetrics:
    def test_inc_dlq_archived_callable(self) -> None:
        from workers.metrics_exporter import inc_dlq_archived

        inc_dlq_archived("omni-evidence")
        inc_dlq_archived("unknown")
