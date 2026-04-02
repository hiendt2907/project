"""Prophet (nếu cài) hoặc fallback tuyến tính + dải tin cậy giả — DataFrame ds/y → yhat / lower / upper."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from anomaly.forecast import linear_forecast_horizon

logger = logging.getLogger(__name__)


def horizons_to_periods(horizon_hours: float, step: str) -> int:
    """Ước số bước dự báo từ horizon (giờ) và ``step`` PromQL (vd ``5m``)."""
    s = (step or "5m").strip().lower()
    sec = 300.0
    if s.endswith("m") and len(s) > 1 and s[:-1].replace(".", "", 1).isdigit():
        sec = float(s[:-1]) * 60.0
    elif s.endswith("s") and len(s) > 1 and s[:-1].replace(".", "", 1).isdigit():
        sec = float(s[:-1])
    elif s.endswith("h") and len(s) > 1 and s[:-1].replace(".", "", 1).isdigit():
        sec = float(s[:-1]) * 3600.0
    return max(1, int(float(horizon_hours) * 3600.0 / max(sec, 1.0)))


def step_to_pandas_freq(step: str) -> str:
    """``5m`` → ``5min``, ``30s`` → ``30s`` (pandas offset)."""
    s = (step or "5m").strip().lower()
    if s.endswith("m") and len(s) > 1:
        num = s[:-1]
        if num.replace(".", "", 1).isdigit():
            return f"{int(float(num))}min"
    if s.endswith("s") and len(s) > 1:
        num = s[:-1]
        if num.replace(".", "", 1).isdigit():
            return f"{int(float(num))}S"
    if s.endswith("h") and len(s) > 1:
        num = s[:-1]
        if num.replace(".", "", 1).isdigit():
            return f"{int(float(num))}H"
    return "5min"


def _infer_freq_td(ds: pd.Series) -> pd.Timedelta:
    t = pd.to_datetime(ds, utc=True)
    if len(t) < 2:
        return pd.Timedelta(minutes=5)
    d = t.diff().dropna()
    if d.empty:
        return pd.Timedelta(minutes=5)
    sec = float(d.median().total_seconds())
    return pd.Timedelta(seconds=max(sec, 1.0))


def _fallback_forecast(
    df: pd.DataFrame,
    periods: int,
    *,
    freq_td: pd.Timedelta,
) -> pd.DataFrame:
    y = df["y"].astype(float).values
    pred_y, _meta = linear_forecast_horizon(y, horizon_steps=periods)
    last = pd.to_datetime(df["ds"].iloc[-1], utc=True)
    future_ds = [last + freq_td * (i + 1) for i in range(periods)]
    std = float(np.nanstd(y)) if len(y) > 1 else 0.0
    band = max(1.96 * std, (abs(float(y[-1])) * 0.05 + 1e-9))
    yhat = np.asarray(pred_y, dtype=float)
    return pd.DataFrame(
        {
            "ds": future_ds,
            "yhat": yhat,
            "yhat_lower": yhat - band,
            "yhat_upper": yhat + band,
        }
    )


def _prepare_work(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < 2:
        raise ValueError("cần ít nhất 2 điểm ds/y")
    work = df[["ds", "y"]].copy()
    work["ds"] = pd.to_datetime(work["ds"], utc=True)
    work["y"] = pd.to_numeric(work["y"], errors="coerce")
    work = work.dropna(subset=["y"])
    if len(work) < 2:
        raise ValueError("sau khi lọc NaN cần ít nhất 2 điểm")
    return work


def forecast_backend_used(
    df: pd.DataFrame,
    periods: int,
    *,
    freq: str | None = None,
) -> tuple[pd.DataFrame, str]:
    """
    Trả về **chỉ phần tương lai** ``ds``, ``yhat``, ``yhat_lower``, ``yhat_upper`` và backend đã dùng.
    """
    work = _prepare_work(df)
    freq_td = _infer_freq_td(work["ds"])
    pandas_freq = freq or step_to_pandas_freq(f"{int(freq_td.total_seconds())}s")

    try:
        from prophet import Prophet  # noqa: PLC0415

        m = Prophet()
        m.fit(work)
        future = m.make_future_dataframe(periods=periods, freq=pandas_freq)
        fcst = m.predict(future)
        last_train = work["ds"].max()
        future_only = fcst[fcst["ds"] > last_train].sort_values("ds")
        if len(future_only) > periods:
            future_only = future_only.head(periods)
        out = future_only[["ds", "yhat", "yhat_lower", "yhat_upper"]].copy()
        out["ds"] = pd.to_datetime(out["ds"], utc=True)
        return out, "prophet"
    except Exception as e:
        logger.warning("prophet unavailable, linear fallback: %s", e)
        return _fallback_forecast(work, periods, freq_td=freq_td), "linear_fallback"


def forecast_metric(
    df: pd.DataFrame,
    periods: int,
    *,
    freq: str | None = None,
) -> pd.DataFrame:
    """API đơn giản — chỉ DataFrame dự báo tương lai."""
    out, _backend = forecast_backend_used(df, periods, freq=freq)
    return out


def forecast_metric_meta(
    df: pd.DataFrame,
    periods: int,
    *,
    freq: str | None = None,
) -> dict[str, Any]:
    """JSON-friendly: ``forecast`` + ``backend``."""
    out, backend = forecast_backend_used(df, periods, freq=freq)
    return {"forecast": out, "backend": backend}
