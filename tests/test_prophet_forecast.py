"""anomaly.prophet_forecast — Prophet hoặc fallback."""

from __future__ import annotations

import pandas as pd

from anomaly.prophet_forecast import (
    forecast_backend_used,
    horizons_to_periods,
    step_to_pandas_freq,
)


def test_horizons_to_periods() -> None:
    assert horizons_to_periods(1.0, "5m") == 12
    assert horizons_to_periods(2.0, "30s") == 240


def test_step_to_pandas_freq() -> None:
    assert "min" in step_to_pandas_freq("5m")


def test_forecast_backend_returns_future_rows() -> None:
    df = pd.DataFrame(
        {
            "ds": pd.date_range("2024-01-01", periods=20, freq="5min", tz="UTC"),
            "y": range(20),
        }
    )
    out, backend = forecast_backend_used(df, periods=5)
    assert len(out) == 5
    assert list(out.columns) == ["ds", "yhat", "yhat_lower", "yhat_upper"]
    assert backend in ("prophet", "linear_fallback")
