"""Tests for workers.vm_timeseries_helpers (no unittest.mock)."""

from __future__ import annotations

import pytest

from workers.vm_timeseries_helpers import prometheus_timeseries_to_line_chart_png_bytes


def test_prometheus_timeseries_to_line_chart_png_bytes_ok() -> None:
    ts = [1_700_000_000.0, 1_700_003_600.0]
    vals = [10.0, 20.0]
    png = prometheus_timeseries_to_line_chart_png_bytes(ts, vals, title="t")
    assert png.startswith(b"\x89PNG")


def test_prometheus_timeseries_to_line_chart_png_bytes_length_mismatch() -> None:
    with pytest.raises(ValueError, match="cùng độ dài"):
        prometheus_timeseries_to_line_chart_png_bytes([1.0], [1.0, 2.0], title="x")


def test_prometheus_timeseries_to_line_chart_png_bytes_empty_values() -> None:
    with pytest.raises(ValueError, match="cùng độ dài"):
        prometheus_timeseries_to_line_chart_png_bytes([], [], title="x")
