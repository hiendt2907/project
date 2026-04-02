"""Prometheus query_range JSON → pandas DataFrame (ds, y)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from workers.analytics_ts import parse_prometheus_matrix_first_series


def matrix_json_to_dataframe(vm_json: dict[str, Any]) -> pd.DataFrame:
    """
    Lấy **series đầu tiên** từ matrix (nhiều series → gộp PromQL phía user, vd ``sum(...)``).
    ``ds``: datetime64 UTC; ``y``: float.
    """
    ts, vals, nser = parse_prometheus_matrix_first_series(vm_json)
    if not ts:
        return pd.DataFrame(columns=["ds", "y"])
    ds = pd.to_datetime(ts, unit="s", utc=True)
    return pd.DataFrame({"ds": ds, "y": [float(v) for v in vals]})


async def fetch_range_dataframe(
    ctx: Any,
    *,
    promql: str,
    start: str,
    end: str,
    step: str,
) -> pd.DataFrame:
    """Gọi ``/api/v1/query_range`` qua ``_prometheus_get_json`` (cùng URL/env với các tool khác)."""
    from workers.sdk_service_tools import _prometheus_get_json

    data = await _prometheus_get_json(
        ctx,
        "/api/v1/query_range",
        {"query": promql, "start": start, "end": end, "step": step},
    )
    if data.get("status") != "success":
        raise ValueError("Prometheus trả status != success")
    return matrix_json_to_dataframe(data)
