"""System Health Manifest: compact instant, manifest build, drift, hint."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fakeredis import FakeAsyncRedis

from workers.baseline_snapshot import (
    REDIS_KEY_SNAPSHOT,
    REDIS_KEY_TS,
    BASELINE_HINT_LEGEND,
    _compact_instant,
    _compute_chs,
    _cpu_drift,
    _extract_cpu_from_old_snapshot,
    _infer_latency_ms_from_prom_value,
    _manifest_json_under_budget,
    _parse_chs_weights_json,
    _sigma_dr,
    baseline_sync_loop,
    build_health_manifest_dict,
    fetch_baseline_snapshot_hint,
    fetch_baseline_system_prompt,
    parse_baseline_promql_lines,
)


def test_compact_instant_vector() -> None:
    data = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {"__name__": "up", "job": "prometheus"},
                    "value": [1.0, "1"],
                }
            ],
        },
    }
    c = _compact_instant(data)
    assert c["n"] == 1
    assert c["top"][0]["v"] == "1"


def test_parse_baseline_promql_lines() -> None:
    raw = """
# cmt
cpu|avg(up)
mem|sum(foo)
badline
"""
    pairs = parse_baseline_promql_lines(raw)
    assert pairs == [("cpu", "avg(up)"), ("mem", "sum(foo)")]


def test_cpu_drift_relative() -> None:
    assert _cpu_drift(0.1, 0.26, threshold=0.15) is True
    assert _cpu_drift(0.1, 0.11, threshold=0.15) is False


def test_sigma_dr_threshold() -> None:
    assert _sigma_dr(4.0, 0.5, threshold=3.0) is True
    assert _sigma_dr(1.0, -3.5, threshold=3.0) is True
    assert _sigma_dr(1.0, 1.0, threshold=3.0) is False
    assert _sigma_dr(None, None, threshold=3.0) is False


def test_infer_latency_ms_from_prom_value() -> None:
    assert _infer_latency_ms_from_prom_value(0.05) == 50.0
    assert _infer_latency_ms_from_prom_value(150.0) == 150.0


def test_parse_chs_weights_json() -> None:
    assert _parse_chs_weights_json("") == {}
    assert _parse_chs_weights_json('{"cpu":0.5,"MEM":0.5}') == {"cpu": 0.5, "mem": 0.5}


def test_compute_chs() -> None:
    w = {"cpu": 1.0, "mem": 1.0, "disk": 0.0, "net": 0.0}
    assert _compute_chs(w, 2.0, -2.0, None, None) == 4.0


def test_extract_cpu_from_old_snapshot() -> None:
    assert _extract_cpu_from_old_snapshot(json.dumps({"cpu": 0.05}).encode()) == 0.05
    assert _extract_cpu_from_old_snapshot(json.dumps({"queries": {"cpu_busy": "0.03"}})) == 0.03


def test_manifest_json_under_budget_trims_evt() -> None:
    big_evt = [{"ns": "a", "r": "r", "m": "x" * 200, "o": "Pod/p"} for _ in range(5)]
    m = {"t": 1, "cpu": 0.1, "evt": big_evt, "dr": False}
    s = _manifest_json_under_budget(m, 400)
    assert len(s) <= 400
    o = json.loads(s.rstrip("…") if s.endswith("…") else s)
    assert len(o.get("evt", [])) <= 3


@pytest.mark.asyncio
async def test_fetch_baseline_snapshot_hint_has_legend() -> None:
    r = FakeAsyncRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"t": 1, "cpu": 0.1}))
    await r.set(REDIS_KEY_TS, "12345")
    out = await fetch_baseline_snapshot_hint(r, max_chars=2000)
    assert BASELINE_HINT_LEGEND in out
    assert "ts=12345" in out


@pytest.mark.asyncio
async def test_fetch_baseline_system_prompt_truncates_total() -> None:
    r = FakeAsyncRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"t": 1, "cpu": 0.1}))
    out = await fetch_baseline_system_prompt(r, max_chars=500)
    assert len(out) <= 500
    assert out.startswith("[SYSTEM BASELINE CONTEXT (LAST 5 MINS)]: ")


@pytest.mark.asyncio
async def test_build_health_manifest_dict_and_drift() -> None:
    async def fake_prom(ctx: object, path: str, params: dict) -> dict:
        q = (params.get("query") or "").strip()
        if q == "omni:node_cpu:z":
            return _vec("0.5")
        if q == "omni:mem:z":
            return _vec("0.2")
        if q == "omni:node_disk:z":
            return _vec("0.1")
        if q == "omni:health:cpu_seasonal_drift_z":
            return _vec("0.3")
        if "node_cpu_seconds_total" in q and "idle" in q:
            return _vec("0.10")
        if "MemAvailable" in q:
            return _vec("0.7")
        if "receive_bytes" in q:
            return _vec("1000")
        if "transmit_bytes" in q:
            return _vec("900")
        if "filesystem_avail" in q:
            return _vec("0.2")
        if "disk_read_bytes" in q:
            return _vec("11")
        if "disk_written_bytes" in q:
            return _vec("22")
        if "reads_completed" in q:
            return _vec("3")
        if "writes_completed" in q:
            return _vec("4")
        if "container_cpu_usage_seconds_total" in q and "multi-agent" in q:
            return _vec("0.5")
        if "resource_requests" in q and "cpu" in q:
            return _vec("1")
        if "working_set_bytes" in q and "multi-agent" in q:
            return _vec("1e9")
        if "resource_requests" in q and "memory" in q:
            return _vec("2e9")
        return {"status": "success", "data": {"resultType": "vector", "result": []}}

    def _vec(val: str) -> dict:
        return {
            "status": "success",
            "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1.0, val]}]},
        }

    class W:
        k8s_default_namespace = "multi-agent"
        baseline_promql = ""
        baseline_warning_events_max = 5
        baseline_warning_events_fetch_limit = 50
        baseline_k8s_events_timeout_sec = 5.0
        baseline_cpu_drift_threshold = 0.15
        baseline_dr_z_threshold = 3.0
        baseline_promql_z_cpu = "omni:node_cpu:z"
        baseline_promql_z_mem = "omni:mem:z"
        baseline_promql_z_disk = "omni:node_disk:z"
        baseline_promql_z_net = ""
        baseline_promql_seasonal_cpu = "omni:health:cpu_seasonal_drift_z"
        golden_latency_promql = ""
        latency_threshold_ms = None
        chs_weights = ""
        chs_threshold = 10.0
        baseline_legacy_cpu_drift_for_dr = False

    ctx = MagicMock()
    ctx.settings = W()

    fake_ev = MagicMock()
    fake_ev.metadata.namespace = "multi-agent"
    fake_ev.reason = "Failed"
    fake_ev.message = "oops"
    inv = MagicMock()
    inv.kind = "Pod"
    inv.name = "p1"
    fake_ev.involved_object = inv
    fake_ev.last_timestamp = None
    fake_ev.event_time = None
    fake_ev.metadata.creation_timestamp = None

    list_ret = MagicMock()
    list_ret.items = [fake_ev]

    with (
        patch("workers.baseline_snapshot._prometheus_get_json", side_effect=fake_prom),
        patch("workers.baseline_snapshot._kube_warning_events", new_callable=AsyncMock, return_value=[]),
    ):
        m = await build_health_manifest_dict(ctx, W(), None)
    assert m["cpu"] == 0.1
    assert m["z_cpu"] == 0.5
    assert m["z_mem"] == 0.2
    assert m["z_disk"] == 0.1
    assert m.get("seasonal_drift_z") == 0.3
    assert m["remediation_silent"] is False
    assert m["dr"] is False
    assert m["rp"]["c"] is not None
    assert "net" in m and "rx" in m["net"]

    old = json.dumps({"cpu": 0.1}).encode()
    with (
        patch("workers.baseline_snapshot._prometheus_get_json", side_effect=fake_prom),
        patch("workers.baseline_snapshot._kube_warning_events", new_callable=AsyncMock, return_value=[]),
    ):
        m2 = await build_health_manifest_dict(ctx, W(), old)
    assert m2["dr"] is False

    async def fake_prom_high(ctx: object, path: str, params: dict) -> dict:
        q = (params.get("query") or "").strip()
        if q == "omni:node_cpu:z":
            return _vec("4.0")
        if q == "omni:mem:z":
            return _vec("0.1")
        if q == "omni:node_disk:z":
            return _vec("0.0")
        if q == "omni:health:cpu_seasonal_drift_z":
            return _vec("0.0")
        if "node_cpu_seconds_total" in q and "idle" in q:
            return _vec("0.30")
        return await fake_prom(ctx, path, params)

    with (
        patch("workers.baseline_snapshot._prometheus_get_json", side_effect=fake_prom_high),
        patch("workers.baseline_snapshot._kube_warning_events", new_callable=AsyncMock, return_value=[]),
    ):
        m3 = await build_health_manifest_dict(ctx, W(), json.dumps({"cpu": 0.1}).encode())
    assert m3["dr"] is True
    assert m3["z_cpu"] == 4.0


@pytest.mark.asyncio
async def test_baseline_sync_loop_writes_redis() -> None:
    import asyncio

    r = FakeAsyncRedis(decode_responses=True)
    stop = asyncio.Event()

    async def fake_prom(ctx: object, path: str, params: dict) -> dict:
        q = (params.get("query") or "").strip()
        if q == "omni:node_cpu:z":
            return {
                "status": "success",
                "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1.0, "0"]}]},
            }
        if q == "omni:mem:z":
            return {
                "status": "success",
                "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1.0, "0"]}]},
            }
        if q == "omni:node_disk:z":
            return {
                "status": "success",
                "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1.0, "0"]}]},
            }
        if q == "omni:health:cpu_seasonal_drift_z":
            return {
                "status": "success",
                "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1.0, "0"]}]},
            }
        return {
            "status": "success",
            "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1.0, "1"]}]},
        }

    class W:
        baseline_snapshot_enabled = True
        baseline_snapshot_interval_sec = 300
        baseline_snapshot_redis_ttl_sec = 600
        baseline_manifest_max_chars = 1400
        baseline_promql = ""
        k8s_default_namespace = "multi-agent"
        baseline_warning_events_max = 5
        baseline_warning_events_fetch_limit = 50
        baseline_k8s_events_timeout_sec = 5.0
        baseline_cpu_drift_threshold = 0.15
        baseline_dr_z_threshold = 3.0
        baseline_promql_z_cpu = "omni:node_cpu:z"
        baseline_promql_z_mem = "omni:mem:z"
        baseline_promql_z_disk = "omni:node_disk:z"
        baseline_promql_z_net = ""
        baseline_promql_seasonal_cpu = "omni:health:cpu_seasonal_drift_z"
        golden_latency_promql = ""
        latency_threshold_ms = None
        chs_weights = ""
        chs_threshold = 10.0
        baseline_legacy_cpu_drift_for_dr = False

    class Ctx:
        settings = W()
        redis = r
        scout_ready = asyncio.Event()
        scout_ready.set()

    ctx = Ctx()
    with (
        patch("workers.baseline_snapshot._prometheus_get_json", side_effect=fake_prom),
        patch("workers.baseline_snapshot._kube_warning_events", new_callable=AsyncMock, return_value=[]),
    ):
        t = asyncio.create_task(baseline_sync_loop(ctx, stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(t, timeout=2.0)

    raw = await r.get(REDIS_KEY_SNAPSHOT)
    assert raw
    data = json.loads(raw)
    assert "t" in data or "cpu" in data
    assert "cpu" in data
    assert len(raw) <= 1400
    ttl = await r.ttl(REDIS_KEY_SNAPSHOT)
    assert ttl > 0
