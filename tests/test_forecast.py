"""anomaly.forecast — scipy linregress + OOM risk."""

from __future__ import annotations

from anomaly.forecast import linear_forecast_horizon, oom_risk_from_series, series_step_seconds


def test_linear_forecast_horizon() -> None:
    pred, meta = linear_forecast_horizon([0.0, 1.0, 2.0, 3.0], horizon_steps=3)
    assert pred.size == 3
    assert "slope" in meta


def test_series_step_seconds() -> None:
    ts = [100.0, 400.0, 700.0]
    assert series_step_seconds(ts) == 300.0


def test_oom_risk_usage() -> None:
    total = 10 * (1024**3)
    # tăng dần usage — gần ngưỡng
    vals = [float(i * 0.9 * (1024**3)) for i in range(8)]
    out = oom_risk_from_series(
        vals,
        total_ram_bytes=total,
        step_seconds=300.0,
        horizon_hours=6.0,
        kind="usage",
        usage_warn_ratio=0.5,
    )
    assert out.get("ok") is True
    assert "oom_or_pressure_risk" in out


def test_oom_risk_insufficient() -> None:
    out = oom_risk_from_series([1.0], total_ram_bytes=8e9, step_seconds=60, horizon_hours=1)
    assert out.get("ok") is False
