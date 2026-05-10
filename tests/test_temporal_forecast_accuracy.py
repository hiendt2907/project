"""Tests for TemporalMetric rate-of-change and forecast_at accuracy."""

from __future__ import annotations

import pytest

from prober.temporal_evidence import TemporalEvidenceBlock, TemporalMetric


# ---------------------------------------------------------------------------
# TemporalMetric — core calculations
# ---------------------------------------------------------------------------

def test_forecast_at_exact_60_minutes():
    """
    values=[(0.0, 10.0), (60.0, 20.0)] — timestamps in seconds, values in arbitrary units.
    rate = (20 - 10) / (60s / 60) = 10.0 per minute
    forecast_at(60) = 20.0 + 10.0 * 60 = 620.0
    """
    metric = TemporalMetric("cpu_percent", [(0.0, 10.0), (60.0, 20.0)])
    result = metric.forecast_at(60)
    assert result is not None
    assert abs(result - 620.0) < 1e-9, f"Expected 620.0, got {result}"


def test_rate_of_change_linear():
    """rate_of_change = (v1 - v0) / ((t1 - t0) / 60) per minute."""
    metric = TemporalMetric("mem_mb", [(0.0, 100.0), (120.0, 160.0)])
    rate = metric.rate_of_change()
    # minutes = 120s / 60 = 2.0 min; delta = 60MB; rate = 30 MB/min
    assert rate is not None
    assert abs(rate - 30.0) < 1e-9


def test_current_value_is_last_point():
    metric = TemporalMetric("rps", [(0.0, 5.0), (30.0, 8.0), (60.0, 11.0)])
    assert metric.current_value() == 11.0


def test_forecast_at_zero_minutes_is_current():
    """forecast_at(0) should equal current_value since rate * 0 = 0."""
    metric = TemporalMetric("latency_ms", [(0.0, 50.0), (60.0, 80.0)])
    assert abs(metric.forecast_at(0) - 80.0) < 1e-9


def test_single_point_returns_none_rate():
    metric = TemporalMetric("disk_io", [(0.0, 42.0)])
    assert metric.rate_of_change() is None
    assert metric.forecast_at(10) is None


def test_empty_metric_returns_none():
    metric = TemporalMetric("error_rate", [])
    assert metric.current_value() is None
    assert metric.rate_of_change() is None
    assert metric.forecast_at(30) is None


def test_forecast_negative_trend():
    """Declining metric: forecast should be lower than current."""
    metric = TemporalMetric("cpu", [(0.0, 80.0), (60.0, 70.0)])
    # rate = (70 - 80) / 1 min = -10 per min
    result = metric.forecast_at(5)
    assert result is not None
    assert result == 70.0 + (-10.0) * 5  # = 20.0


def test_sample_points_count():
    metric = TemporalMetric("mem", [(0.0, 1.0), (10.0, 2.0), (20.0, 3.0)])
    assert metric.sample_points == 3


# ---------------------------------------------------------------------------
# TemporalEvidenceBlock — aggregate
# ---------------------------------------------------------------------------

def test_block_sample_points_sums_metrics():
    block = TemporalEvidenceBlock("test_probe", namespace="ns", pod="pod-x")
    block.add_metric("cpu", [(0.0, 10.0), (60.0, 20.0)])
    block.add_metric("mem", [(0.0, 100.0), (60.0, 110.0), (120.0, 120.0)])
    assert block.sample_points == 5


def test_block_to_prompt_block_contains_metric():
    block = TemporalEvidenceBlock("prometheus", namespace="prod")
    block.add_metric("cpu_pct", [(0.0, 10.0), (60.0, 20.0)])
    prompt = block.to_prompt_block()
    assert "cpu_pct" in prompt
    assert "[TEMPORAL_EVIDENCE" in prompt


def test_block_empty_metrics_returns_empty_header():
    block = TemporalEvidenceBlock("empty_probe")
    prompt = block.to_prompt_block()
    # Even with no metrics, header line is emitted
    assert "[TEMPORAL_EVIDENCE" in prompt


def test_forecast_linearly_covers_standard_horizons():
    block = TemporalEvidenceBlock("prom", namespace="x")
    block.add_metric("rps", [(0.0, 100.0), (60.0, 110.0)])
    forecasts = block.forecast_linearly(hours=24)
    assert 60 in forecasts   # 1h
    assert 180 in forecasts  # 3h
    assert 1440 in forecasts # 24h
    assert all(isinstance(v, dict) for v in forecasts.values())
