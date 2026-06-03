"""Prompt Variant A/B Testing (S3.3).

Deterministically assigns traces to prompt variants based on trace_id hash.
Tracks outcomes (JSON compliance, steps, success) in Redis.
Evaluates a winner after MIN_TRACES_PER_VARIANT samples each.

Redis schema:
  omni:prompt:ab:{variant}  → HSET with: total, json_ok, steps_sum, success
  omni:prompt:ab:winner     → STR: "A" | "B" (set after evaluation)
  omni:prompt:ab:winner_at  → STR: float timestamp

Variants:
  A: compact catalog (5 tools), 2-sentence thoughts, schema embedded in prompt
  B: full catalog (15 tools), 3-sentence thoughts, schema reference only
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

_AB_KEY_FMT = "omni:prompt:ab:{variant}"
_WINNER_KEY = "omni:prompt:ab:winner"
_WINNER_AT_KEY = "omni:prompt:ab:winner_at"
_MIN_TRACES_PER_VARIANT = 100  # Evaluate after this many traces each

VARIANTS: dict[str, dict[str, Any]] = {
    "A": {
        "tool_catalog_size": 5,
        "thought_max_sentences": 2,
        "schema_embedded": True,
        "description": "Compact: 5 tools, 2-sentence thoughts, schema embedded",
    },
    "B": {
        "tool_catalog_size": 15,
        "thought_max_sentences": 3,
        "schema_embedded": False,
        "description": "Full: 15 tools, 3-sentence thoughts, schema reference only",
    },
}


def assign_variant(trace_id: str) -> str:
    """Deterministic assignment: trace_id hash → variant A or B."""
    h = int(hashlib.sha256(trace_id.encode()).hexdigest(), 16)
    return "A" if h % 2 == 0 else "B"


async def record_outcome(
    redis: Any,
    variant: str,
    *,
    json_ok: bool,
    steps: int,
    success: bool,
) -> None:
    """Record one trace outcome for the given variant."""
    if redis is None or variant not in VARIANTS:
        return
    key = _AB_KEY_FMT.format(variant=variant)
    try:
        pipe = redis.pipeline()
        pipe.hincrby(key, "total", 1)
        if json_ok:
            pipe.hincrby(key, "json_ok", 1)
        pipe.hincrbyfloat(key, "steps_sum", float(steps))
        if success:
            pipe.hincrby(key, "success", 1)
        await pipe.execute()
    except Exception as e:
        logger.debug("ab_test record_outcome fail variant=%s err=%s", variant, e)


async def get_variant_stats(redis: Any, variant: str) -> dict[str, float]:
    """Return stats dict for a variant."""
    key = _AB_KEY_FMT.format(variant=variant)
    try:
        raw = await redis.hgetall(key)
        if not raw:
            return {}
        def _f(k: str) -> float:
            v = raw.get(k.encode()) or raw.get(k) or 0
            return float(v)
        total = _f("total")
        return {
            "total": total,
            "json_ok": _f("json_ok"),
            "json_ok_rate": _f("json_ok") / total if total > 0 else 0.0,
            "steps_sum": _f("steps_sum"),
            "avg_steps": _f("steps_sum") / total if total > 0 else 0.0,
            "success": _f("success"),
            "success_rate": _f("success") / total if total > 0 else 0.0,
        }
    except Exception as e:
        logger.debug("ab_test get_stats fail variant=%s err=%s", variant, e)
        return {}


async def evaluate_winner(redis: Any) -> str | None:
    """Evaluate if we have a winner. Returns variant letter or None if inconclusive.

    Criteria: both variants need MIN_TRACES_PER_VARIANT samples, then compare
    JSON compliance rate and success rate (weighted 60/40).
    """
    if redis is None:
        return None
    try:
        # Check existing winner.
        existing = await redis.get(_WINNER_KEY)
        if existing:
            return (existing.decode() if isinstance(existing, bytes) else str(existing)).strip()

        stats_a = await get_variant_stats(redis, "A")
        stats_b = await get_variant_stats(redis, "B")

        if stats_a.get("total", 0) < _MIN_TRACES_PER_VARIANT:
            return None
        if stats_b.get("total", 0) < _MIN_TRACES_PER_VARIANT:
            return None

        # Weighted score: 60% JSON compliance + 40% success.
        score_a = 0.6 * stats_a.get("json_ok_rate", 0) + 0.4 * stats_a.get("success_rate", 0)
        score_b = 0.6 * stats_b.get("json_ok_rate", 0) + 0.4 * stats_b.get("success_rate", 0)

        if abs(score_a - score_b) < 0.05:
            return None  # Too close — not statistically conclusive

        winner = "A" if score_a > score_b else "B"
        import time as _time
        await redis.set(_WINNER_KEY, winner)
        await redis.set(_WINNER_AT_KEY, str(_time.time()))
        logger.info(
            "event=ab_winner_declared winner=%s score_a=%.3f score_b=%.3f",
            winner, score_a, score_b,
        )
        return winner
    except Exception as e:
        logger.debug("ab_test evaluate_winner fail err=%s", e)
        return None
