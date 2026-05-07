"""Dự báo nhẹ (pandas/scipy) — xu hướng RAM / nguy cơ áp ngưỡng, không ML nặng."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy.stats import linregress

MetricKind = Literal["usage", "available"]


MIN_R_SQUARED_DEFAULT = 0.3  # overridden by OMNI_FORECAST_MIN_R_SQUARED in settings


def linear_forecast_horizon(
    values: list[float] | np.ndarray,
    *,
    horizon_steps: int,
    min_r_squared: float = MIN_R_SQUARED_DEFAULT,
) -> tuple[np.ndarray, dict[str, Any]]:
    """
    Hồi quy tuyến tính trên chỉ số 0..n-1, dự báo horizon_steps điểm tiếp theo.
    Trả về (mảng giá trị dự báo, dict slope/intercept/r2/low_confidence).
    low_confidence=True khi r_squared < min_r_squared — callers should not escalate risk.
    """
    y = np.asarray(values, dtype=float)
    if y.size < 2:
        raise ValueError("cần ít nhất 2 điểm")
    x = np.arange(y.size, dtype=float)
    res = linregress(x, y)
    pred_x = np.arange(y.size, y.size + horizon_steps, dtype=float)
    pred_y = res.slope * pred_x + res.intercept
    r_squared = float(res.rvalue**2)
    meta: dict[str, Any] = {
        "slope": float(res.slope),
        "intercept": float(res.intercept),
        "r_value": float(res.rvalue),
        "r_squared": r_squared,
        "low_confidence": r_squared < min_r_squared,
    }
    return pred_y, meta


def oom_risk_from_series(
    values: list[float] | np.ndarray,
    *,
    total_ram_bytes: float,
    step_seconds: float,
    horizon_hours: float,
    kind: MetricKind = "usage",
    usage_warn_ratio: float = 0.92,
) -> dict[str, Any]:
    """
    Dự báo xu hướng từ chuỗi (byte). `usage`: RAM đã dùng tăng → nguy cơ khi dự báo > usage_warn_ratio * total.
    `available`: RAM còn lại giảm → nguy cơ khi dự báo < (1-usage_warn_ratio)*total.
    """
    y = np.asarray(values, dtype=float)
    if y.size < 3 or total_ram_bytes <= 0:
        return {
            "ok": False,
            "reason": "insufficient_data_or_total",
            "points": int(y.size),
        }
    horizon_steps = max(1, int(horizon_hours * 3600.0 / max(step_seconds, 1.0)))
    pred_y, meta = linear_forecast_horizon(y, horizon_steps=horizon_steps)
    last_pred = float(pred_y[-1])

    if meta.get("low_confidence"):
        return {
            "ok": True,
            "metric_kind": kind,
            "horizon_hours": float(horizon_hours),
            "oom_or_pressure_risk": False,
            "low_confidence": True,
            "headline": f"Forecast skipped: r_squared={meta['r_squared']:.3f} below threshold.",
            "regression": meta,
        }

    if kind == "usage":
        threshold = usage_warn_ratio * total_ram_bytes
        risk = last_pred >= threshold
        headline = (
            f"Dự báo sau ~{horizon_hours:.1f}h: usage ≈ {last_pred / (1024**3):.2f} GiB "
            f"(ngưỡng cảnh báo {usage_warn_ratio:.0%} ≈ {threshold / (1024**3):.2f} GiB)."
        )
    else:
        floor = (1.0 - usage_warn_ratio) * total_ram_bytes
        risk = last_pred <= floor
        headline = (
            f"Dự báo sau ~{horizon_hours:.1f}h: MemAvailable ≈ {last_pred / (1024**3):.2f} GiB "
            f"(sàn an toàn ~{floor / (1024**3):.2f} GiB)."
        )

    return {
        "ok": True,
        "metric_kind": kind,
        "horizon_hours": float(horizon_hours),
        "horizon_steps": horizon_steps,
        "step_seconds": float(step_seconds),
        "last_observed_gib": float(y[-1] / (1024**3)),
        "predicted_last_gib": float(last_pred / (1024**3)),
        "oom_or_pressure_risk": bool(risk),
        "low_confidence": False,
        "headline": headline,
        "regression": meta,
    }


def series_step_seconds(timestamps: list[float]) -> float:
    """Khoảng cách trung bình giữa các mốc thời gian (unix)."""
    if len(timestamps) < 2:
        return 300.0
    ts = np.asarray(timestamps, dtype=float)
    d = np.diff(np.sort(ts))
    positive = d[d > 0]
    if positive.size == 0:
        return 300.0
    return float(np.median(positive))


def pandas_trend_forecast(
    values: list[float] | np.ndarray,
    *,
    horizon_steps: int,
) -> tuple[np.ndarray, dict[str, Any]]:
    """
    Xu hướng tuyến tính: thống kê pandas trên lịch sử + dự báo ``linear_forecast_horizon``.
    Trả về mảng giá trị dự báo (độ dài ``horizon_steps``) và meta (mean/std/slope/...).
    """
    y = np.asarray(values, dtype=float)
    if y.size < 2:
        raise ValueError("cần ít nhất 2 điểm")
    s = pd.Series(y, dtype=float)
    pred_y, reg = linear_forecast_horizon(y, horizon_steps=horizon_steps)
    meta: dict[str, Any] = {
        "mean": float(s.mean()),
        "std": float(s.std(ddof=0)),
        "min": float(s.min()),
        "max": float(s.max()),
        "last_observed": float(s.iloc[-1]),
        "regression": reg,
    }
    return pred_y, meta


def forecast_horizon_steps(duration_label: str, step_seconds: float, *, cap: int = 200) -> int:
    """Chuyển '1h' / '30m' thành số bước dự báo theo ``step_seconds``."""
    d = (duration_label or "1h").strip().lower()
    sec = 3600.0
    if d.endswith("h") and len(d) > 1:
        try:
            sec = float(d[:-1]) * 3600.0
        except ValueError:
            sec = 3600.0
    elif d.endswith("m") and len(d) > 1:
        try:
            sec = float(d[:-1]) * 60.0
        except ValueError:
            sec = 3600.0
    steps = int(sec / max(step_seconds, 1.0))
    return max(1, min(steps, cap))
