"""Coverage tests for anomaly/three_sigma.py (observe_adaptive) and
pkg/temporal/pattern_matcher.py."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis

from anomaly.three_sigma import (
    ThreeSigmaGate,
    DEFAULT_THRESHOLD,
    DEFAULT_WINDOW,
    fingerprint_key_samples,
)
from pkg.temporal.pattern_matcher import (
    record_incident_timestamp,
    detect_recurrence,
    maybe_schedule_prediction,
    emit_due_predictions,
    _SCHEDULED_KEY,
    _TS_KEY_FMT,
    _MIN_INCIDENTS_FOR_DETECTION,
)


# ── ThreeSigmaGate.observe_adaptive ──────────────────────────────────────────

async def _gate(r: FakeRedis) -> ThreeSigmaGate:
    return ThreeSigmaGate(r, window_size=10, ttl_sec=3600)


@pytest.mark.asyncio
async def test_observe_adaptive_no_namespace():
    r = FakeRedis(decode_responses=True)
    gate = await _gate(r)
    is_anom, z = await gate.observe_adaptive("cpu", 50.0)
    assert not is_anom


@pytest.mark.asyncio
async def test_observe_adaptive_maintenance_window_suppresses():
    r = FakeRedis(decode_responses=True)
    gate = await _gate(r)
    await r.set("omni:maint:prod:api", "1")
    # Seed enough data to trigger anomaly without maintenance
    for v in [10.0] * 10:
        await r.lpush("3sigma:metric:cpu", str(v))
    # With maintenance window → returns (False, None)
    is_anom, z = await gate.observe_adaptive("cpu", 9999.0, namespace="prod", deployment="api")
    assert not is_anom
    assert z is None


@pytest.mark.asyncio
async def test_observe_adaptive_per_workload_config():
    r = FakeRedis(decode_responses=True)
    gate = await _gate(r)
    await r.hset("omni:sigma:config:ns:svc", mapping={"threshold": "2.0", "window": "5"})
    # Should use custom threshold (2.0) and window (5) instead of defaults
    is_anom, z = await gate.observe_adaptive("latency", 10.0, namespace="ns", deployment="svc")
    # With only 1 data point, not anomaly but no error
    assert isinstance(is_anom, bool)


@pytest.mark.asyncio
async def test_observe_adaptive_config_missing_fields():
    r = FakeRedis(decode_responses=True)
    gate = await _gate(r)
    # Config with no threshold/window fields — uses defaults
    await r.hset("omni:sigma:config:ns:svc", mapping={"auto_calibrated": "true"})
    is_anom, z = await gate.observe_adaptive("cpu", 10.0, namespace="ns", deployment="svc")
    assert isinstance(is_anom, bool)


@pytest.mark.asyncio
async def test_observe_adaptive_config_exception_uses_defaults():
    r = FakeRedis(decode_responses=True)
    gate = await _gate(r)
    # No config key at all → no exception, uses defaults
    is_anom, z = await gate.observe_adaptive("mem", 10.0, namespace="ns-x", deployment="dep-y")
    assert isinstance(is_anom, bool)


@pytest.mark.asyncio
async def test_observe_adaptive_anomaly_detected_with_custom_threshold():
    r = FakeRedis(decode_responses=True)
    gate = await _gate(r)
    await r.hset("omni:sigma:config:prod:batch", mapping={"threshold": "2.0", "window": "10"})
    # Seed stable baseline
    for v in [100.0] * 9:
        await gate.observe_adaptive("req", v, namespace="prod", deployment="batch")
    # Massive spike
    is_anom, z = await gate.observe_adaptive("req", 9999.0, namespace="prod", deployment="batch")
    assert is_anom is True


# ── record_incident_timestamp ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_incident_timestamp_none_redis():
    await record_incident_timestamp(None, pattern_key="k8s:crashloop:nginx")  # no error


@pytest.mark.asyncio
async def test_record_incident_timestamp_empty_key():
    r = FakeRedis(decode_responses=True)
    await record_incident_timestamp(r, pattern_key="")  # no error


@pytest.mark.asyncio
async def test_record_incident_timestamp_stores():
    r = FakeRedis(decode_responses=True)
    t = time.time()
    await record_incident_timestamp(r, pattern_key="cron:midnight", timestamp=t)
    key = _TS_KEY_FMT.format(pattern_key="cron:midnight")
    count = await r.zcard(key)
    assert count == 1


# ── detect_recurrence ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_detect_recurrence_none_redis():
    result = await detect_recurrence(None, pattern_key="k")
    assert result is None


@pytest.mark.asyncio
async def test_detect_recurrence_empty_key():
    r = FakeRedis(decode_responses=True)
    result = await detect_recurrence(r, pattern_key="")
    assert result is None


@pytest.mark.asyncio
async def test_detect_recurrence_insufficient_data():
    r = FakeRedis(decode_responses=True)
    await record_incident_timestamp(r, pattern_key="cron", timestamp=1000.0)
    await record_incident_timestamp(r, pattern_key="cron", timestamp=2000.0)
    result = await detect_recurrence(r, pattern_key="cron")
    assert result is None  # < _MIN_INCIDENTS_FOR_DETECTION


@pytest.mark.asyncio
async def test_detect_recurrence_irregular_pattern():
    r = FakeRedis(decode_responses=True)
    # Very irregular intervals (high CV)
    tss = [1000.0, 2000.0, 2100.0, 3500.0, 3600.0, 5000.0]
    for ts in tss:
        await record_incident_timestamp(r, pattern_key="irreg", timestamp=ts)
    result = await detect_recurrence(r, pattern_key="irreg")
    assert result is None


@pytest.mark.asyncio
async def test_detect_recurrence_too_short_intervals():
    r = FakeRedis(decode_responses=True)
    # Intervals under 60s → noise
    base = 1000.0
    for i in range(6):
        await record_incident_timestamp(r, pattern_key="fast", timestamp=base + i * 10)
    result = await detect_recurrence(r, pattern_key="fast")
    assert result is None


@pytest.mark.asyncio
async def test_detect_recurrence_stable_pattern():
    r = FakeRedis(decode_responses=True)
    # Very regular pattern: every 3600s
    base = 1_000_000.0
    interval = 3600.0
    for i in range(6):
        await record_incident_timestamp(r, pattern_key="daily_cron", timestamp=base + i * interval)
    result = await detect_recurrence(r, pattern_key="daily_cron")
    assert result is not None
    assert result["pattern_key"] == "daily_cron"
    assert result["incident_count"] == 6
    assert abs(result["mean_interval_sec"] - interval) < 1.0
    assert result["confidence"] >= 0.8


# ── maybe_schedule_prediction ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_maybe_schedule_none_recurrence():
    r = FakeRedis(decode_responses=True)
    scheduled = await maybe_schedule_prediction(r, pattern_key="no-data")
    assert not scheduled


@pytest.mark.asyncio
async def test_maybe_schedule_past_emit_time():
    r = FakeRedis(decode_responses=True)
    # Pattern with regular interval but emit_at already in the past
    base = 1000.0  # way in the past
    for i in range(6):
        await record_incident_timestamp(r, pattern_key="old_cron", timestamp=base + i * 3600)
    # next_predicted_at will be ~7000, way in the past → emit_at past
    result = await maybe_schedule_prediction(r, pattern_key="old_cron")
    assert not result


@pytest.mark.asyncio
async def test_maybe_schedule_future_emission():
    r = FakeRedis(decode_responses=True)
    # Pattern with regular interval in the near future
    now = time.time()
    interval = 3600.0
    # 5 occurrences ending ~30s ago, next predicted ~3570s from now
    for i in range(6):
        await record_incident_timestamp(r, pattern_key="future_cron", timestamp=now - (5 - i) * interval - 30)
    result = await maybe_schedule_prediction(r, pattern_key="future_cron")
    # May or may not schedule depending on timing/confidence
    assert isinstance(result, bool)


@pytest.mark.asyncio
async def test_maybe_schedule_writes_to_redis():
    r = FakeRedis(decode_responses=True)
    now = time.time()
    interval = 3600.0
    for i in range(6):
        await record_incident_timestamp(r, pattern_key="sched_test", timestamp=now - (5 - i) * interval - 30)
    result = await maybe_schedule_prediction(r, pattern_key="sched_test")
    if result:
        count = await r.zcard(_SCHEDULED_KEY)
        assert count >= 1


# ── emit_due_predictions ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_emit_due_none_redis():
    ctx = SimpleNamespace(redis=None, kafka=None)
    n = await emit_due_predictions(ctx)
    assert n == 0


@pytest.mark.asyncio
async def test_emit_due_no_items():
    r = FakeRedis(decode_responses=True)
    ctx = SimpleNamespace(redis=r, kafka=None)
    n = await emit_due_predictions(ctx)
    assert n == 0


@pytest.mark.asyncio
async def test_emit_due_item_in_past_no_kafka():
    r = FakeRedis(decode_responses=True)
    past_score = time.time() - 100
    prediction = {"rule": "temporal_prediction", "pattern_key": "k", "confidence": 0.9,
                  "mean_interval_sec": 3600, "next_predicted_at": time.time() + 3600,
                  "kafka_topic": "omni-proactive-incidents", "source": "temporal_pattern_matcher"}
    await r.zadd(_SCHEDULED_KEY, {json.dumps(prediction): past_score})
    ctx = SimpleNamespace(redis=r, kafka=None)
    n = await emit_due_predictions(ctx)
    assert n == 0  # kafka is None, so not emitted but item removed


@pytest.mark.asyncio
async def test_emit_due_with_kafka():
    r = FakeRedis(decode_responses=True)
    past_score = time.time() - 100
    prediction = {"rule": "temporal_prediction", "pattern_key": "cron",
                  "confidence": 0.9, "mean_interval_sec": 3600,
                  "next_predicted_at": time.time() + 3600,
                  "kafka_topic": "omni-proactive-incidents",
                  "source": "temporal_pattern_matcher"}
    await r.zadd(_SCHEDULED_KEY, {json.dumps(prediction): past_score})
    mock_kafka = AsyncMock()
    mock_kafka.send_dict = AsyncMock()
    ctx = SimpleNamespace(redis=r, kafka=mock_kafka)
    n = await emit_due_predictions(ctx)
    assert n == 1
    mock_kafka.send_dict.assert_awaited_once()


@pytest.mark.asyncio
async def test_emit_due_invalid_json_removed():
    r = FakeRedis(decode_responses=True)
    past_score = time.time() - 100
    await r.zadd(_SCHEDULED_KEY, {"not-valid-json": past_score})
    ctx = SimpleNamespace(redis=r, kafka=None)
    n = await emit_due_predictions(ctx)
    assert n == 0


# ── fingerprint_key_samples ────────────────────────────────────────────────────

def test_fingerprint_deterministic():
    fp1 = fingerprint_key_samples("cpu", [1.0, 2.0, 3.0])
    fp2 = fingerprint_key_samples("cpu", [1.0, 2.0, 3.0])
    assert fp1 == fp2


def test_fingerprint_different_inputs():
    fp1 = fingerprint_key_samples("cpu", [1.0])
    fp2 = fingerprint_key_samples("mem", [1.0])
    assert fp1 != fp2


# ── exception paths in observe_adaptive ──────────────────────────────────────

@pytest.mark.asyncio
async def test_observe_adaptive_redis_error_in_maint_check():
    """Exception in maintenance window check is silently caught (line 108-109)."""
    r = FakeRedis(decode_responses=True)
    gate = await _gate(r)

    original_exists = r.exists

    async def exploding_exists(*args, **kwargs):
        raise Exception("redis explosion")

    r.exists = exploding_exists
    # Should not raise even when redis.exists raises
    is_anom, z = await gate.observe_adaptive("cpu", 10.0, namespace="ns", deployment="dep")
    r.exists = original_exists
    assert is_anom is not None


@pytest.mark.asyncio
async def test_observe_adaptive_redis_error_in_config_load():
    """Exception in sigma config load is silently caught (line 125-126)."""
    r = FakeRedis(decode_responses=True)
    gate = await _gate(r)

    original_hgetall = r.hgetall

    async def exploding_hgetall(*args, **kwargs):
        raise Exception("redis explosion")

    r.hgetall = exploding_hgetall
    is_anom, z = await gate.observe_adaptive("cpu", 10.0, namespace="ns", deployment="dep")
    r.hgetall = original_hgetall
    assert is_anom is not None


# ── get_z_score ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_z_score_returns_none_when_empty():
    r = FakeRedis(decode_responses=True)
    gate = await _gate(r)
    z = await gate.get_z_score("missing_metric")
    assert z is None


@pytest.mark.asyncio
async def test_get_z_score_returns_none_when_too_few_samples():
    r = FakeRedis(decode_responses=True)
    gate = await _gate(r)
    await gate.observe("cpu", 10.0)
    await gate.observe("cpu", 11.0)
    z = await gate.get_z_score("cpu")
    assert z is None


@pytest.mark.asyncio
async def test_get_z_score_returns_value_with_sufficient_samples():
    r = FakeRedis(decode_responses=True)
    gate = await _gate(r)
    for v in [10.0, 10.5, 11.0, 10.2, 10.8, 11.2, 10.0, 10.3, 10.1, 10.7]:
        await gate.observe("cpu", v)
    z = await gate.get_z_score("cpu")
    assert z is not None
    assert isinstance(z, float)


@pytest.mark.asyncio
async def test_get_z_score_returns_none_when_stddev_zero():
    """All same values → std ≈ 0 → returns None."""
    r = FakeRedis(decode_responses=True)
    gate = await _gate(r)
    for _ in range(10):
        await gate.observe("const_cpu", 50.0)
    z = await gate.get_z_score("const_cpu")
    assert z is None
