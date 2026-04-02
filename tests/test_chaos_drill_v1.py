"""Unit tests for scripts/chaos_drill_v1 helpers (no cluster)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "chaos_drill_v1",
    Path(__file__).resolve().parent.parent / "scripts" / "chaos_drill_v1.py",
)
assert _SPEC and _SPEC.loader
cd = importlib.util.module_from_spec(_SPEC)
sys.modules["chaos_drill_v1"] = cd
_SPEC.loader.exec_module(cd)


def test_parse_prometheus_instant_vector() -> None:
    data = {
        "status": "success",
        "data": {
            "result": [{"metric": {"__name__": "x"}, "value": [1234.5, "2.5"]}],
        },
    }
    assert cd.parse_prometheus_instant(data) == (1234.5, 2.5)


def test_parse_prometheus_instant_empty() -> None:
    assert cd.parse_prometheus_instant({"status": "success", "data": {"result": []}}) is None


def test_is_sample_fresh() -> None:
    assert cd.is_sample_fresh(100.0, 150.0, 90.0) is True
    assert cd.is_sample_fresh(100.0, 200.0, 90.0) is False


def test_warn_low_sigma_prints(capsys: pytest.CaptureFixture[str]) -> None:
    assert cd.warn_low_sigma(0.005) is True
    err = capsys.readouterr().err
    assert "[LOW_SIGMA]" in err
    assert cd.warn_low_sigma(0.5) is False


def test_self_awareness_clear() -> None:
    assert cd.self_awareness_heuristic_pass("CLEAR something") is True


def test_self_awareness_rollout_bad() -> None:
    assert cd.self_awareness_heuristic_pass('{"tool":"k8s_rollout_restart"}') is False


def test_self_awareness_keywords() -> None:
    assert cd.self_awareness_heuristic_pass("missing ConfigMap ref") is True


@pytest.mark.asyncio
async def test_loki_query_range_lines_parses() -> None:
    from unittest.mock import AsyncMock, MagicMock

    body = {
        "data": {
            "result": [
                {
                    "values": [
                        ["1000000000", "line one"],
                        ["2000000000", "line two"],
                    ],
                }
            ],
        },
    }
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=body)
    hc = MagicMock()
    hc.get = AsyncMock(return_value=resp)

    lines = await cd.loki_query_range_lines(
        hc, "http://loki:3100", logql="{x=~`y`}", start_sec=0, end_sec=10
    )
    assert lines == ["line one", "line two"]
