"""metrics.prometheus_dataframe — matrix JSON → DataFrame."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from metrics.prometheus_dataframe import fetch_range_dataframe, matrix_json_to_dataframe


def test_matrix_json_to_dataframe_first_series() -> None:
    j = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {"job": "x"}, "values": [[1000.0, "1.5"], [1060.0, "2.5"]]}],
        },
    }
    df = matrix_json_to_dataframe(j)
    assert len(df) == 2
    assert list(df.columns) == ["ds", "y"]
    assert df["y"].tolist() == [1.5, 2.5]


@pytest.mark.asyncio
async def test_fetch_range_dataframe_uses_prometheus_get_json() -> None:
    vm = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {}, "values": [[100.0, "1"], [200.0, "2"]]}],
        },
    }
    ctx = MagicMock()
    with patch("workers.sdk_service_tools._prometheus_get_json", new_callable=AsyncMock, return_value=vm):
        df = await fetch_range_dataframe(
            ctx,
            promql="up",
            start="now-1h",
            end="now",
            step="30s",
        )
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 2
