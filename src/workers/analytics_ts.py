"""Time-series: pandas/numpy — thống kê, MA, dự đoán tuyến tính (SDK-only)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def parse_prometheus_matrix_first_series(vm_json: dict[str, Any]) -> tuple[list[float], list[float], int]:
    """
    Lấy series đầu tiên từ Prometheus /api/v1/query[_range] JSON (matrix hoặc vector).
    Trả về (timestamps_unix, values_float, total_series).
    """
    data = vm_json.get("data") or {}
    rtype = data.get("resultType", "")
    res = data.get("result") or []
    n = len(res)
    if not res:
        return [], [], 0
    first = res[0]
    if rtype == "matrix":
        pairs = first.get("values") or []
        ts: list[float] = []
        vals: list[float] = []
        for p in pairs:
            if len(p) >= 2:
                ts.append(float(p[0]))
                vals.append(float(p[1]))
        return ts, vals, n
    if rtype == "vector":
        v = first.get("value")
        if v and len(v) >= 2:
            return [float(v[0])], [float(v[1])], n
    return [], [], n


# Tên cũ (VM) — giữ alias cho test/module import.
parse_vm_matrix_first_series = parse_prometheus_matrix_first_series


def analyze_series(
    values: list[float],
    *,
    ma_window: int = 0,
    forecast_steps: int = 0,
) -> dict[str, Any]:
    """
    Thống kê mô tả, moving average (pandas), dự đoán bậc 1 (numpy.polyfit) cho forecast_steps bước tiếp.
    """
    if not values:
        return {"error": "values rỗng"}
    s = pd.Series(values, dtype=float)
    out: dict[str, Any] = {
        "n": int(len(s)),
        "mean": float(s.mean()),
        "std": float(s.std(ddof=0)),
        "min": float(s.min()),
        "max": float(s.max()),
        "p50": float(s.quantile(0.5)),
        "p95": float(s.quantile(0.95)),
    }
    if ma_window and ma_window >= 2 and len(s) >= ma_window:
        ma = s.rolling(window=ma_window, min_periods=ma_window).mean().dropna()
        out["moving_average"] = [float(x) for x in ma.tolist()]
        out["ma_window"] = ma_window
    if forecast_steps and forecast_steps > 0 and len(values) >= 2:
        x = np.arange(len(values), dtype=float)
        y = np.asarray(values, dtype=float)
        a, b = np.polyfit(x, y, 1)
        future_x = np.arange(len(values), len(values) + forecast_steps, dtype=float)
        future_y = a * future_x + b
        out["forecast_linear"] = {
            "slope": float(a),
            "intercept": float(b),
            "next_values": [float(v) for v in future_y],
        }
        out["forecast_steps"] = forecast_steps
    return out
