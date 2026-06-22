"""Tests for metrics.prometheus_dataframe.matrix_json_to_dataframe (no unittest.mock)."""

from __future__ import annotations

import pandas as pd

from metrics.prometheus_dataframe import matrix_json_to_dataframe


def test_matrix_json_to_dataframe_empty() -> None:
    df = matrix_json_to_dataframe({"data": {"resultType": "matrix", "result": []}})
    assert list(df.columns) == ["ds", "y"]
    assert len(df) == 0


def test_matrix_json_to_dataframe_matrix_first_series() -> None:
    payload = {
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"job": "x"},
                    "values": [[1000.0, "1.5"], [1060.0, "2.5"]],
                },
                {"metric": {"job": "y"}, "values": [[0.0, "9.0"]]},
            ],
        }
    }
    df = matrix_json_to_dataframe(payload)
    assert len(df) == 2
    assert df["y"].tolist() == [1.5, 2.5]
    assert pd.api.types.is_datetime64_any_dtype(df["ds"].dtype)


def test_matrix_json_to_dataframe_vector() -> None:
    payload = {
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [2000.0, "42"]}],
        }
    }
    df = matrix_json_to_dataframe(payload)
    assert len(df) == 1
    assert float(df["y"].iloc[0]) == 42.0
