"""Unit tests for pure helpers in workers.baseline_snapshot (W3 coverage)."""

from __future__ import annotations

import json
import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from workers import baseline_snapshot as bs


def test_promql_builders_strings():
    assert "node_cpu_seconds_total" in bs._prom_cpu_busy()
    assert "MemAvailable_bytes" in bs._prom_mem_avail_ratio()
    assert "node_network_receive_bytes_total" in bs._prom_net_rx()
    assert "node_network_transmit_bytes_total" in bs._prom_net_tx()
    assert "node_filesystem_avail_bytes" in bs._prom_dsk_usage_ratio()
    assert "fstype!~" in bs._prom_dsk_usage_ratio()
    assert "node_disk_read_bytes_total" in bs._prom_dsk_read_bps()
    assert "node_disk_written_bytes_total" in bs._prom_dsk_write_bps()
    assert "node_disk_reads_completed_total" in bs._prom_dsk_read_iops()
    assert "node_disk_writes_completed_total" in bs._prom_dsk_write_iops()


def test_promql_namespace_helpers():
    ns = "prod-ns"
    assert f'namespace="{ns}"' in bs._prom_cpu_usage_ns(ns)
    assert "container_cpu_usage_seconds_total" in bs._prom_cpu_usage_ns(ns)
    assert 'resource="cpu"' in bs._prom_cpu_requests_ns(ns)
    assert "container_memory_working_set_bytes" in bs._prom_mem_working_ns(ns)
    assert 'resource="memory"' in bs._prom_mem_requests_ns(ns)


def test_parse_baseline_promql_lines():
    raw = """
# comment
cpu|rate(foo[1m])

  mem  |  bar  
bad_no_pipe
|empty_name
name_only|
"""
    out = bs.parse_baseline_promql_lines(raw)
    assert out == [("cpu", "rate(foo[1m])"), ("mem", "bar")]


def test_compact_instant_truncates_and_shapes():
    rows = []
    for i in range(50):
        rows.append(
            {
                "metric": {"__name__": f"m{i}", "pod": "p"},
                "value": [1, str(i)],
            }
        )
    data = {"data": {"result": rows}}
    c = bs._compact_instant(data)
    assert c["n"] == 50
    assert len(c["top"]) == 25
    assert c["top"][0]["metric"] == "m0"


def test_compact_instant_empty():
    assert bs._compact_instant({}) == {"n": 0, "top": []}
    assert bs._compact_instant({"data": {}}) == {"n": 0, "top": []}


def test_instant_to_scalar_str():
    assert bs._instant_to_scalar_str({"data": {"result": []}}) is None
    one = {"data": {"result": [{"value": [1, "0.42"]}]}}
    assert bs._instant_to_scalar_str(one) == "0.42"
    multi = {
        "data": {
            "result": [
                {"value": [1, "a"]},
                {"value": [2, "b"]},
            ]
        }
    }
    s = bs._instant_to_scalar_str(multi)
    assert json.loads(s) == ["a", "b"]


def test_round_num():
    assert bs._round_num(None) is None
    assert bs._round_num(float("nan")) is None
    assert bs._round_num(float("inf")) is None
    assert bs._round_num(1.234567, nd=2) == 1.23
    assert bs._round_num(3, nd=4) == 3.0


def test_infer_latency_ms_from_prom_value():
    assert bs._infer_latency_ms_from_prom_value(0.05) == 50.0
    assert bs._infer_latency_ms_from_prom_value(150.0) == 150.0
    assert bs._infer_latency_ms_from_prom_value(float("nan")) is None


def test_parse_chs_weights_json():
    assert bs._parse_chs_weights_json("") == {}
    assert bs._parse_chs_weights_json("  ") == {}
    assert bs._parse_chs_weights_json("not json") == {}
    assert bs._parse_chs_weights_json('["x"]') == {}
    raw = '{"CPU": 1.5, "mem": 2, "bad": "x", "nan": NaN}'
    # JSON doesn't allow NaN in strict json.loads — use valid floats
    w = bs._parse_chs_weights_json('{"CPU": 1, "mem": 2.5}')
    assert w == {"cpu": 1.0, "mem": 2.5}


def test_compute_chs():
    weights = {"cpu": 1.0, "mem": 2.0}
    assert bs._compute_chs({}, 1.0, 1.0, 1.0, 1.0) is None
    v = bs._compute_chs(weights, 2.0, -3.0, None, 1.0)
    # weights only cpu+mem — net/disk keys absent from weights dict
    assert v == bs._round_num(1.0 * 2.0 + 2.0 * 3.0, nd=4)
    assert bs._compute_chs({"cpu": 1.0}, "bad", None, None, None) == 0.0
    nan_v = bs._compute_chs({"cpu": 1.0}, float("nan"), None, None, None)
    assert nan_v == 0.0


def test_extract_cpu_from_old_snapshot():
    assert bs._extract_cpu_from_old_snapshot(None) is None
    assert bs._extract_cpu_from_old_snapshot(b"not json") is None
    j = json.dumps({"cpu": 0.31})
    assert bs._extract_cpu_from_old_snapshot(j) == 0.31
    assert bs._extract_cpu_from_old_snapshot(j.encode()) == 0.31
    q = json.dumps({"queries": {"cpu_busy": "0.99"}})
    assert bs._extract_cpu_from_old_snapshot(q) == 0.99
    assert bs._extract_cpu_from_old_snapshot(json.dumps({"queries": {"cpu_busy": "x"}})) is None


def test_cpu_drift():
    assert bs._cpu_drift(None, 1.0, threshold=0.1) is False
    assert bs._cpu_drift(0.5, 0.5, threshold=0.1) is False
    assert bs._cpu_drift(0.1, 0.5, threshold=0.1, eps=1e-9) is True
    assert bs._cpu_drift(1.0, 1.35, threshold=0.4) is False
    assert bs._cpu_drift(1.0, 1.5, threshold=0.3) is True


def test_sigma_dr():
    assert bs._sigma_dr(None, None, threshold=3.0) is False
    assert bs._sigma_dr(3.1, None, threshold=3.0) is True
    assert bs._sigma_dr(None, -3.01, threshold=3.0) is True
    assert bs._sigma_dr(2.9, 2.9, threshold=3.0) is False


def test_manifest_json_under_budget_trims():
    manifest = {
        "t": 1,
        "evt": [{"ns": "a", "r": "r", "m": "x" * 200, "o": "o"} for _ in range(10)],
        "q": {"a": "b"},
        "golden": {"x": 1},
        "w": {"cpu": 1.0},
        "dsk": {"u": 1, "rt": 2, "wt": 3, "ri": 4, "wi": 5},
    }
    s = bs._manifest_json_under_budget(manifest, 5000)
    assert len(s) <= 5000
    tiny = bs._manifest_json_under_budget(dict(manifest), max_chars=80)
    assert len(tiny) <= 80 or tiny.endswith("…")


@pytest.mark.asyncio
async def test_supplemental_queries_empty():
    assert await bs._supplemental_queries(object(), "") == {}
    assert await bs._supplemental_queries(object(), "# only\n") == {}


@pytest.mark.asyncio
async def test_query_scalar_str_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_json(ctx: object, path: str, params: dict) -> dict:
        return {"status": "success", "data": {"result": [{"value": [1, "9.9"]}]}}

    monkeypatch.setattr(bs, "_prometheus_get_json", fake_get_json)
    assert await bs._query_scalar_str(None, "up") == "9.9"


@pytest.mark.asyncio
async def test_query_scalar_str_not_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_json(ctx: object, path: str, params: dict) -> dict:
        return {"status": "error"}

    monkeypatch.setattr(bs, "_prometheus_get_json", fake_get_json)
    assert await bs._query_scalar_str(None, "up") is None


@pytest.mark.asyncio
async def test_query_scalar_str_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*a: object, **k: object) -> dict:
        raise RuntimeError("net")

    monkeypatch.setattr(bs, "_prometheus_get_json", boom)
    assert await bs._query_scalar_str(None, "up") is None


@pytest.mark.asyncio
async def test_query_float_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(ctx: object, path: str, params: dict) -> dict:
        return {"status": "success", "data": {"result": [{"value": [1, "0.25"]}]}}

    monkeypatch.setattr(bs, "_prometheus_get_json", ok)
    assert await bs._query_float(None, "x") == 0.25

    async def bad_val(ctx: object, path: str, params: dict) -> dict:
        return {"status": "success", "data": {"result": [{"value": [1, "nope"]}]}}

    monkeypatch.setattr(bs, "_prometheus_get_json", bad_val)
    assert await bs._query_float(None, "x") is None


@pytest.mark.asyncio
async def test_fetch_baseline_system_prompt() -> None:
    r = MagicMock()
    r.get = AsyncMock(return_value=json.dumps({"cpu": 0.2}).encode())
    out = await bs.fetch_baseline_system_prompt(r, 800)
    assert out.startswith("[SYSTEM BASELINE CONTEXT")
    assert "cpu" in out


@pytest.mark.asyncio
async def test_fetch_baseline_system_prompt_truncates() -> None:
    r = MagicMock()
    r.get = AsyncMock(return_value="x" * 500)
    out = await bs.fetch_baseline_system_prompt(r, max_chars=60)
    assert len(out) <= 60
    assert out.endswith("…")


@pytest.mark.asyncio
async def test_fetch_baseline_system_prompt_errors() -> None:
    r = MagicMock()
    r.get = AsyncMock(side_effect=RuntimeError("redis down"))
    assert await bs.fetch_baseline_system_prompt(r, 100) == ""


@pytest.mark.asyncio
async def test_fetch_baseline_snapshot_hint() -> None:
    r = MagicMock()
    r.get = AsyncMock(side_effect=[json.dumps({"cpu": 1}), "999"])
    out = await bs.fetch_baseline_snapshot_hint(r, 5000)
    assert "ts=999" in out
    assert bs.BASELINE_HINT_LEGEND[:20] in out


@pytest.mark.asyncio
async def test_fetch_baseline_snapshot_hint_short_budget() -> None:
    r = MagicMock()
    r.get = AsyncMock(side_effect=[b'{"cpu":1}', None])
    out = await bs.fetch_baseline_snapshot_hint(r, max_chars=80)
    assert out.endswith("…") or len(out) <= 80
