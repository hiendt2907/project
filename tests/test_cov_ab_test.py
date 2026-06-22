"""Coverage tests for pkg/prompt_optimizer/ab_test.py."""
from __future__ import annotations

import pytest
from fakeredis.aioredis import FakeRedis

from pkg.prompt_optimizer.ab_test import (
    VARIANTS,
    _MIN_TRACES_PER_VARIANT,
    assign_variant,
    evaluate_winner,
    get_variant_stats,
    record_outcome,
)


# ── assign_variant ────────────────────────────────────────────────────────────

def test_assign_variant_deterministic():
    assert assign_variant("trace-abc") == assign_variant("trace-abc")


def test_assign_variant_returns_valid():
    for i in range(20):
        assert assign_variant(f"trace-{i}") in ("A", "B")


def test_assign_variant_both_sides():
    results = {assign_variant(f"t-{i}") for i in range(100)}
    assert "A" in results and "B" in results


# ── record_outcome ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_outcome_increments_total():
    r = FakeRedis(decode_responses=True)
    await record_outcome(r, "A", json_ok=True, steps=3, success=True)
    raw = await r.hgetall("omni:prompt:ab:A")
    assert int(raw["total"]) == 1
    assert int(raw["json_ok"]) == 1
    assert int(raw["success"]) == 1


@pytest.mark.asyncio
async def test_record_outcome_no_json_ok():
    r = FakeRedis(decode_responses=True)
    await record_outcome(r, "B", json_ok=False, steps=5, success=False)
    raw = await r.hgetall("omni:prompt:ab:B")
    assert int(raw["total"]) == 1
    assert raw.get("json_ok") is None
    assert raw.get("success") is None


@pytest.mark.asyncio
async def test_record_outcome_none_redis_skipped():
    await record_outcome(None, "A", json_ok=True, steps=1, success=True)  # no error


@pytest.mark.asyncio
async def test_record_outcome_invalid_variant_skipped():
    r = FakeRedis(decode_responses=True)
    await record_outcome(r, "Z", json_ok=True, steps=1, success=True)
    raw = await r.hgetall("omni:prompt:ab:Z")
    assert raw == {}


@pytest.mark.asyncio
async def test_record_outcome_multiple_accumulate():
    r = FakeRedis(decode_responses=True)
    for _ in range(3):
        await record_outcome(r, "A", json_ok=True, steps=2, success=True)
    raw = await r.hgetall("omni:prompt:ab:A")
    assert int(raw["total"]) == 3
    assert float(raw["steps_sum"]) == 6.0


# ── get_variant_stats ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_variant_stats_empty():
    r = FakeRedis(decode_responses=True)
    stats = await get_variant_stats(r, "A")
    assert stats == {}


@pytest.mark.asyncio
async def test_get_variant_stats_after_records():
    r = FakeRedis(decode_responses=True)
    await record_outcome(r, "A", json_ok=True, steps=4, success=True)
    await record_outcome(r, "A", json_ok=False, steps=2, success=False)
    stats = await get_variant_stats(r, "A")
    assert stats["total"] == 2.0
    assert stats["json_ok"] == 1.0
    assert stats["json_ok_rate"] == 0.5
    assert stats["avg_steps"] == 3.0
    assert stats["success_rate"] == 0.5


# ── evaluate_winner ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_evaluate_winner_none_redis():
    result = await evaluate_winner(None)
    assert result is None


@pytest.mark.asyncio
async def test_evaluate_winner_insufficient_samples():
    r = FakeRedis(decode_responses=True)
    await record_outcome(r, "A", json_ok=True, steps=1, success=True)
    result = await evaluate_winner(r)
    assert result is None


@pytest.mark.asyncio
async def test_evaluate_winner_existing_winner():
    r = FakeRedis(decode_responses=True)
    await r.set("omni:prompt:ab:winner", "B")
    result = await evaluate_winner(r)
    assert result == "B"


@pytest.mark.asyncio
async def test_evaluate_winner_a_wins():
    r = FakeRedis(decode_responses=True)
    # Seed enough data: 100+ samples each, A clearly better
    key_a = "omni:prompt:ab:A"
    key_b = "omni:prompt:ab:B"
    n = _MIN_TRACES_PER_VARIANT
    await r.hset(key_a, mapping={"total": str(n), "json_ok": str(n), "steps_sum": str(n), "success": str(n)})
    await r.hset(key_b, mapping={"total": str(n), "json_ok": "0",    "steps_sum": str(n), "success": "0"})
    result = await evaluate_winner(r)
    assert result == "A"


@pytest.mark.asyncio
async def test_evaluate_winner_b_wins():
    r = FakeRedis(decode_responses=True)
    n = _MIN_TRACES_PER_VARIANT
    await r.hset("omni:prompt:ab:A", mapping={"total": str(n), "json_ok": "0",    "steps_sum": str(n), "success": "0"})
    await r.hset("omni:prompt:ab:B", mapping={"total": str(n), "json_ok": str(n), "steps_sum": str(n), "success": str(n)})
    result = await evaluate_winner(r)
    assert result == "B"


@pytest.mark.asyncio
async def test_evaluate_winner_too_close_returns_none():
    r = FakeRedis(decode_responses=True)
    n = _MIN_TRACES_PER_VARIANT
    # Both have identical stats → abs(score_a - score_b) < 0.05
    mapping = {"total": str(n), "json_ok": str(n // 2), "steps_sum": str(n), "success": str(n // 2)}
    await r.hset("omni:prompt:ab:A", mapping=mapping)
    await r.hset("omni:prompt:ab:B", mapping=mapping)
    result = await evaluate_winner(r)
    assert result is None
