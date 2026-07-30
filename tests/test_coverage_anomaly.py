"""Coverage tests for src/anomaly/three_sigma.py and src/anomaly/forecast.py."""
from __future__ import annotations

import pytest
import pytest_asyncio
import fakeredis.aioredis

from anomaly.three_sigma import (
    ThreeSigmaGate,
    _sanitize_metric_id,
    fingerprint_key_samples,
    DEFAULT_WINDOW,
    DEFAULT_TTL_SEC,
    DEFAULT_KEY_PREFIX,
    MIN_STDDEV,
)
from anomaly.forecast import (
    linear_forecast_horizon,
    oom_risk_from_series,
    series_step_seconds,
    pandas_trend_forecast,
    forecast_horizon_steps,
)


# ── _sanitize_metric_id ───────────────────────────────────────────────────────

def test_sanitize_metric_id_clean():
    assert _sanitize_metric_id("cpu.usage") == "cpu.usage"


def test_sanitize_metric_id_colons():
    assert _sanitize_metric_id("ns:pod:metric") == "ns:pod:metric"


def test_sanitize_metric_id_replaces_spaces():
    result = _sanitize_metric_id("cpu usage rate")
    assert " " not in result


def test_sanitize_metric_id_replaces_slashes():
    result = _sanitize_metric_id("k8s/cpu/ns")
    assert "/" not in result
    assert "k8s" in result


def test_sanitize_metric_id_empty_becomes_unknown():
    assert _sanitize_metric_id("   ") == "unknown"


def test_sanitize_metric_id_truncates_at_200():
    long_id = "a" * 250
    assert len(_sanitize_metric_id(long_id)) == 200


# ── fingerprint_key_samples ───────────────────────────────────────────────────

def test_fingerprint_key_samples_returns_16_chars():
    result = fingerprint_key_samples("cpu", [1.0, 2.0, 3.0])
    assert len(result) == 16


def test_fingerprint_key_samples_stable():
    r1 = fingerprint_key_samples("cpu", [1.0, 2.0])
    r2 = fingerprint_key_samples("cpu", [1.0, 2.0])
    assert r1 == r2


def test_fingerprint_key_samples_different_inputs():
    r1 = fingerprint_key_samples("cpu", [1.0, 2.0])
    r2 = fingerprint_key_samples("cpu", [1.0, 3.0])
    assert r1 != r2


def test_fingerprint_key_samples_different_ids():
    r1 = fingerprint_key_samples("cpu", [1.0])
    r2 = fingerprint_key_samples("mem", [1.0])
    assert r1 != r2


# ── ThreeSigmaGate construction ───────────────────────────────────────────────

@pytest.fixture
async def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
async def gate(redis):
    return ThreeSigmaGate(redis, window_size=10, ttl_sec=60)


def test_gate_raises_on_small_window():
    import asyncio
    async def _make():
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        with pytest.raises(ValueError, match="window_size"):
            ThreeSigmaGate(r, window_size=2, ttl_sec=60)
    asyncio.run(_make())


def test_gate_raises_on_zero_ttl():
    import asyncio
    async def _make():
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        with pytest.raises(ValueError, match="ttl_sec"):
            ThreeSigmaGate(r, window_size=10, ttl_sec=0)
    asyncio.run(_make())


def test_gate_raises_on_negative_ttl():
    import asyncio
    async def _make():
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        with pytest.raises(ValueError, match="ttl_sec"):
            ThreeSigmaGate(r, window_size=10, ttl_sec=-1)
    asyncio.run(_make())


# ── ThreeSigmaGate._key ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gate_key_includes_prefix(redis):
    gate = ThreeSigmaGate(redis, window_size=10, ttl_sec=60, key_prefix="test:")
    key = gate._key("cpu")
    assert key.startswith("test:")
    assert "cpu" in key


# ── ThreeSigmaGate.observe — insufficient data ────────────────────────────────

@pytest.mark.asyncio
async def test_observe_returns_false_none_when_too_few_points(redis):
    gate = ThreeSigmaGate(redis, window_size=10, ttl_sec=60)
    is_anomaly, z = await gate.observe("m1", 1.0)
    assert is_anomaly is False
    assert z is None

    is_anomaly, z = await gate.observe("m1", 2.0)
    assert is_anomaly is False
    assert z is None


@pytest.mark.asyncio
async def test_observe_no_anomaly_on_stable_series(redis):
    gate = ThreeSigmaGate(redis, window_size=20, ttl_sec=60)
    # Feed >=9 stable values — qua gate cold-start _MIN_BASELINE=8 (2026-07-31), z tính
    # được và loạt ổn định thì KHÔNG bất thường.
    for v in [10.0, 10.1, 10.0, 9.9, 10.0, 10.1, 10.0, 10.0, 9.9, 10.1]:
        is_anomaly, z = await gate.observe("stable", v)
    assert is_anomaly is False
    assert z is not None


@pytest.mark.asyncio
async def test_observe_detects_clear_anomaly(redis):
    gate = ThreeSigmaGate(redis, window_size=20, ttl_sec=60)
    # Build stable baseline
    for _ in range(15):
        await gate.observe("metric", 50.0)
    # Inject obvious spike
    is_anomaly, z = await gate.observe("metric", 500.0)
    assert is_anomaly is True
    assert z is not None
    assert z > 3.0


@pytest.mark.asyncio
async def test_observe_returns_none_z_when_zero_stddev(redis):
    gate = ThreeSigmaGate(redis, window_size=10, ttl_sec=60)
    # All identical values → stddev=0
    for _ in range(5):
        await gate.observe("flat", 42.0)
    is_anomaly, z = await gate.observe("flat", 42.0)
    assert is_anomaly is False
    assert z is None


# ── ThreeSigmaGate.ttl_for ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ttl_for_returns_negative_two_for_missing_key(redis):
    gate = ThreeSigmaGate(redis, window_size=10, ttl_sec=60)
    ttl = await gate.ttl_for("nonexistent_metric_xyz")
    assert ttl == -2


@pytest.mark.asyncio
async def test_ttl_for_returns_positive_after_observe(redis):
    gate = ThreeSigmaGate(redis, window_size=10, ttl_sec=60)
    await gate.observe("ttl_test", 1.0)
    ttl = await gate.ttl_for("ttl_test")
    assert ttl > 0
    assert ttl <= 60


# ── ThreeSigmaGate.key_count_estimate ────────────────────────────────────────

@pytest.mark.asyncio
async def test_key_count_estimate_zero_initially(redis):
    gate = ThreeSigmaGate(redis, window_size=10, ttl_sec=60, key_prefix="unique_prefix_xyz:")
    count = await gate.key_count_estimate()
    assert count == 0


@pytest.mark.asyncio
async def test_key_count_estimate_counts_after_observe(redis):
    gate = ThreeSigmaGate(redis, window_size=10, ttl_sec=60, key_prefix="counttest:")
    await gate.observe("m1", 1.0)
    await gate.observe("m2", 2.0)
    count = await gate.key_count_estimate()
    assert count == 2


# ── linear_forecast_horizon ────────────────────────────────────────────────────

def test_linear_forecast_horizon_basic():
    import numpy as np
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    pred, meta = linear_forecast_horizon(values, horizon_steps=3)
    assert len(pred) == 3
    assert meta["slope"] > 0
    assert "r_squared" in meta
    assert "low_confidence" in meta


def test_linear_forecast_horizon_raises_on_too_few_points():
    with pytest.raises(ValueError, match="ít nhất"):
        linear_forecast_horizon([1.0], horizon_steps=1)


def test_linear_forecast_horizon_low_confidence_flag():
    import numpy as np
    # Noisy random data → low r_squared
    values = [10.0, 1.0, 8.0, 2.0, 9.0, 1.5, 8.5, 2.5]
    _, meta = linear_forecast_horizon(values, horizon_steps=2, min_r_squared=0.9)
    assert meta["low_confidence"] is True


def test_linear_forecast_horizon_high_confidence_flag():
    values = [i * 10.0 for i in range(10)]  # perfect linear
    _, meta = linear_forecast_horizon(values, horizon_steps=3, min_r_squared=0.3)
    assert meta["low_confidence"] is False
    assert meta["r_squared"] > 0.99


def test_linear_forecast_horizon_returns_numpy_array():
    import numpy as np
    pred, _ = linear_forecast_horizon([1.0, 2.0, 3.0], horizon_steps=5)
    assert isinstance(pred, np.ndarray)
    assert len(pred) == 5


# ── oom_risk_from_series ──────────────────────────────────────────────────────

def test_oom_risk_insufficient_data():
    result = oom_risk_from_series(
        [1.0, 2.0],  # only 2 points, < 3
        total_ram_bytes=8 * 1024**3,
        step_seconds=60.0,
        horizon_hours=1.0,
    )
    assert result["ok"] is False
    assert "insufficient" in result["reason"]


def test_oom_risk_zero_total_ram():
    result = oom_risk_from_series(
        [1.0, 2.0, 3.0],
        total_ram_bytes=0.0,
        step_seconds=60.0,
        horizon_hours=1.0,
    )
    assert result["ok"] is False


def test_oom_risk_usage_kind_at_risk():
    total = 8 * 1024**3  # 8 GiB
    # Linear growth approaching 92% limit
    step = 60.0
    # 100 points, each step adds 50MB (trending toward OOM within 1h)
    start = 6.5 * 1024**3  # 6.5 GiB used
    values = [start + i * 10 * 1024 * 1024 for i in range(20)]
    result = oom_risk_from_series(
        values,
        total_ram_bytes=total,
        step_seconds=step,
        horizon_hours=2.0,
        kind="usage",
    )
    assert result["ok"] is True
    assert "oom_or_pressure_risk" in result


def test_oom_risk_available_kind():
    total = 8 * 1024**3
    # Available memory declining toward floor
    start = 2.0 * 1024**3
    values = [start - i * 30 * 1024 * 1024 for i in range(20)]
    result = oom_risk_from_series(
        values,
        total_ram_bytes=total,
        step_seconds=60.0,
        horizon_hours=1.0,
        kind="available",
    )
    assert result["ok"] is True


def test_oom_risk_low_confidence_skips():
    import numpy as np
    # Noisy data → low r_squared → low_confidence path
    rng = [100.0 + (i % 3) * 50.0 for i in range(20)]
    result = oom_risk_from_series(
        rng,
        total_ram_bytes=8 * 1024**3,
        step_seconds=60.0,
        horizon_hours=1.0,
        kind="usage",
    )
    # Either skips (low_confidence) or has result — just check ok is True
    assert result.get("ok") is True
    if result.get("low_confidence"):
        assert "Forecast skipped" in result.get("headline", "")


def test_oom_risk_no_risk_stable():
    total = 8 * 1024**3
    # Stable usage at 50% — no risk
    values = [4.0 * 1024**3] * 20
    result = oom_risk_from_series(
        values,
        total_ram_bytes=total,
        step_seconds=60.0,
        horizon_hours=1.0,
        kind="usage",
    )
    # stddev=0 → low_confidence=True (all same) or no risk
    assert result["ok"] is True


# ── series_step_seconds ────────────────────────────────────────────────────────

def test_series_step_seconds_returns_default_on_single_point():
    assert series_step_seconds([1234567890.0]) == 300.0


def test_series_step_seconds_empty():
    assert series_step_seconds([]) == 300.0


def test_series_step_seconds_two_points():
    result = series_step_seconds([1000.0, 1060.0])
    assert abs(result - 60.0) < 1.0


def test_series_step_seconds_median():
    # [60, 60, 60, 60, 3600] → median=60
    ts = [0.0, 60.0, 120.0, 180.0, 240.0, 3840.0]
    result = series_step_seconds(ts)
    assert abs(result - 60.0) < 1.0


def test_series_step_seconds_all_same_timestamps():
    # All identical → no positive diffs → fallback to 300
    ts = [1000.0, 1000.0, 1000.0]
    result = series_step_seconds(ts)
    assert result == 300.0


# ── pandas_trend_forecast ─────────────────────────────────────────────────────

def test_pandas_trend_forecast_basic():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    pred, meta = pandas_trend_forecast(values, horizon_steps=3)
    assert len(pred) == 3
    assert "mean" in meta
    assert "std" in meta
    assert "regression" in meta
    assert "last_observed" in meta


def test_pandas_trend_forecast_raises_on_too_few():
    with pytest.raises(ValueError):
        pandas_trend_forecast([1.0], horizon_steps=1)


def test_pandas_trend_forecast_meta_values():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    _, meta = pandas_trend_forecast(values, horizon_steps=2)
    assert abs(meta["mean"] - 30.0) < 1.0
    assert meta["last_observed"] == 50.0
    assert meta["min"] == 10.0
    assert meta["max"] == 50.0


# ── forecast_horizon_steps ─────────────────────────────────────────────────────

def test_forecast_horizon_steps_1h_60s():
    steps = forecast_horizon_steps("1h", 60.0)
    assert steps == 60


def test_forecast_horizon_steps_30m_60s():
    steps = forecast_horizon_steps("30m", 60.0)
    assert steps == 30


def test_forecast_horizon_steps_invalid_label():
    # Falls back to 3600s
    steps = forecast_horizon_steps("invalid", 60.0)
    assert steps == 60


def test_forecast_horizon_steps_empty_label():
    steps = forecast_horizon_steps("", 60.0)
    assert steps == 60


def test_forecast_horizon_steps_caps_at_200():
    # 24h / 1s step = 86400 steps → capped at 200
    steps = forecast_horizon_steps("24h", 1.0)
    assert steps == 200


def test_forecast_horizon_steps_minimum_1():
    # Very large step_seconds
    steps = forecast_horizon_steps("1m", 10000.0)
    assert steps == 1


def test_forecast_horizon_steps_6h():
    steps = forecast_horizon_steps("6h", 300.0)  # 5min steps
    assert steps == 72  # 6*3600/300 = 72


def test_linear_forecast_horizon_raises_on_insufficient_data():
    """Line 63: raises ValueError when < 2 points."""
    from anomaly.forecast import linear_forecast_horizon
    import pytest
    with pytest.raises(ValueError, match="ít nhất 2"):
        linear_forecast_horizon([42.0], horizon_steps=3)
