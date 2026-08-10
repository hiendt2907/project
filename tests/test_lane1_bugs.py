"""Lane 1 (SYS_RESOURCE) production bug regression tests.

Each test targets a specific silent failure that would be invisible
in production without the fix. No happy path tests.

Test report:
  L1-1a: observe_adaptive swallows invalid threshold config → must log WARNING
  L1-1b: observe_adaptive swallows maint_check Redis error → must log WARNING
  L1-2:  corrupt snapshot JSON → must log WARNING (currently silent)
  L1-3:  stale snapshot (>300s) → must log WARNING (currently not checked)
  L1-4:  lrange < 3 samples → must log DEBUG (currently completely silent)
"""
from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fakeredis.aioredis import FakeRedis

import workers.evidence_consumer as ec
from anomaly.three_sigma import (
    ThreeSigmaGate,
    _MAINT_KEY_FMT,
    _SIGMA_CONFIG_KEY_FMT,
)
from workers.baseline_snapshot import REDIS_KEY_SNAPSHOT, REDIS_KEY_TS


# ── L1-1a ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_observe_adaptive_invalid_threshold_logs_warning(caplog):
    """L1-1a: observe_adaptive must log WARNING when hgetall returns non-float threshold.

    Before fix: bare `except Exception: pass` at line 125-126 swallows ValueError
                from float("not-a-float") with no log.
    After fix:  logger.warning("observe_adaptive: config load failed ns=... dep=... err=...").
    """
    redis = FakeRedis(decode_responses=True)
    cfg_key = _SIGMA_CONFIG_KEY_FMT.format(namespace="ns", deployment="dep")
    await redis.hset(cfg_key, mapping={"threshold": "not-a-float"})

    gate = ThreeSigmaGate(redis)

    with caplog.at_level(logging.WARNING, logger="anomaly.three_sigma"):
        await gate.observe_adaptive("cpu", 4.0, namespace="ns", deployment="dep")

    # FAILS before fix (no warning emitted), PASSES after fix
    assert any(
        r.levelno >= logging.WARNING for r in caplog.records
    ), "Expected WARNING about invalid threshold config, got none"


# ── L1-1b ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_observe_adaptive_redis_error_on_maint_check_logs_warning(caplog):
    """L1-1b: observe_adaptive must log WARNING when Redis raises during maint key check.

    Before fix: bare `except Exception: pass` at lines 108-109 swallows
                ConnectionError from redis.exists() with no log.
    After fix:  logger.warning("observe_adaptive: maint_check failed ns=... dep=... err=...").
    """
    redis = FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis)

    with (
        patch.object(redis, "exists", AsyncMock(side_effect=ConnectionError("redis down"))),
        caplog.at_level(logging.WARNING, logger="anomaly.three_sigma"),
    ):
        await gate.observe_adaptive("cpu", 1.0, namespace="ns", deployment="dep")

    # FAILS before fix (swallowed silently), PASSES after fix
    assert any(
        r.levelno >= logging.WARNING for r in caplog.records
    ), "Expected WARNING on Redis error in maint check, got none"


# ── L1-2 ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_proof_of_fault_gate_corrupt_snapshot_logs_warning(caplog):
    """L1-2: _proof_of_fault_gate must log WARNING when snapshot JSON is corrupt.

    Before fix: `except Exception: snap = {}` at lines 679-680 with no log — operator
                sees "sigma gate blocked" without knowing the snapshot was corrupt.
    After fix:  logger.warning("event=baseline_snapshot_corrupt trace=... err=...").
    """
    redis = FakeRedis(decode_responses=True)
    await redis.set(REDIS_KEY_SNAPSHOT, "not-valid-json{{{")

    ctx = SimpleNamespace(
        redis=redis,
        settings=SimpleNamespace(
            baseline_dr_z_threshold=3.0,
            autonomous_sigma_observation_window=1,
            omni_proof_lane_enabled=True,
        ),
    )

    with (
        patch.object(ec, "resolve_proof_lane", return_value=("resource", "test")),
        patch.object(ec, "critical_evidence_present", return_value=False),
        caplog.at_level(logging.WARNING, logger="workers.evidence_consumer"),
    ):
        await ec._proof_of_fault_gate(ctx, trace="trace-corrupt", batch=[])

    # FAILS before fix (no log emitted), PASSES after fix
    assert any(
        "baseline_snapshot_corrupt" in r.message
        for r in caplog.records
        if r.levelno >= logging.WARNING
    ), "Expected 'baseline_snapshot_corrupt' WARNING, got none"


# ── L1-3 ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_proof_of_fault_gate_stale_snapshot_logs_warning(caplog):
    """L1-3: _proof_of_fault_gate must log WARNING when snapshot timestamp is >300s old.

    Before fix: REDIS_KEY_TS is never read in _proof_of_fault_gate — no freshness check.
                Stale z-scores (up to 3599s old) are used for current advisory decisions.
    After fix:  reads REDIS_KEY_TS, logs WARNING "event=baseline_snapshot_stale age_sec=...".
    """
    redis = FakeRedis(decode_responses=True)
    snap = {"z_cpu": 1.5, "z_mem": 1.2, "dr": False}
    await redis.set(REDIS_KEY_SNAPSHOT, json.dumps(snap))
    # Ngưỡng tươi giờ SUY RA từ chu kỳ sync (snapshot_freshness_budget_sec), không còn
    # hằng số 300s — xem docs/handoffs Đ41. interval=60 -> budget=300 (sàn), nên 400s
    # vẫn vượt ngưỡng đúng như test này muốn kiểm.
    await redis.set(REDIS_KEY_TS, str(time.time() - 400))  # 400s ago > 300s budget floor

    ctx = SimpleNamespace(
        redis=redis,
        settings=SimpleNamespace(
            baseline_dr_z_threshold=3.0,
            autonomous_sigma_observation_window=1,
            omni_proof_lane_enabled=True,
            baseline_snapshot_interval_sec=60,
        ),
    )

    with (
        patch.object(ec, "resolve_proof_lane", return_value=("resource", "test")),
        patch.object(ec, "critical_evidence_present", return_value=False),
        caplog.at_level(logging.WARNING, logger="workers.evidence_consumer"),
    ):
        await ec._proof_of_fault_gate(ctx, trace="trace-stale", batch=[])

    # FAILS before fix (REDIS_KEY_TS never read), PASSES after fix
    assert any(
        "baseline_snapshot_stale" in r.message
        for r in caplog.records
        if r.levelno >= logging.WARNING
    ), "Expected 'baseline_snapshot_stale' WARNING, got none"


# ── L1-4 ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_three_sigma_insufficient_samples_logs_debug_not_silent(caplog):
    """L1-4: observe() with < 3 samples must emit a DEBUG log explaining why.

    Before fix: completely silent `return False, None` at lines 80-81.
                Operator can't distinguish cold start from transient Redis error.
    After fix:  logger.debug("3sigma: insufficient_data metric=... samples=... window=...").
    """
    redis = FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis)
    # Fresh key: observe() does 1 LPUSH → only 1 sample total → < 3 minimum

    with caplog.at_level(logging.DEBUG, logger="anomaly.three_sigma"):
        is_anomaly, z = await gate.observe("cpu", 3.0)

    assert not is_anomaly
    assert z is None

    # FAILS before fix (no log at all), PASSES after fix
    assert any(
        "cold_start" in r.message.lower() or "insufficient" in r.message.lower() or "samples" in r.message.lower()
        for r in caplog.records
        if r.levelno == logging.DEBUG
    ), "Expected DEBUG log about insufficient samples, got none"
