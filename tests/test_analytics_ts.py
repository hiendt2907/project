"""analytics_ts pure functions."""

from __future__ import annotations

from workers.analytics_ts import (
    analyze_series,
    parse_prometheus_matrix_first_series,
    parse_vm_matrix_first_series,
)


def test_parse_matrix() -> None:
    vm = {
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"__name__": "x"},
                    "values": [[100.0, "1"], [101.0, "2"]],
                }
            ],
        }
    }
    ts, vals, n = parse_vm_matrix_first_series(vm)
    assert parse_prometheus_matrix_first_series(vm) == (ts, vals, n)
    assert n == 1
    assert ts == [100.0, 101.0]
    assert vals == [1.0, 2.0]


def test_analyze_forecast() -> None:
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = analyze_series(vals, ma_window=3, forecast_steps=2)
    assert out["n"] == 5
    assert "forecast_linear" in out
    assert len(out["forecast_linear"]["next_values"]) == 2
