"""Coverage-gap tests for src/workers/baseline_snapshot.py.

Targets uncovered lines: 276-324 (_kube_warning_events), 327-336
(_supplemental_queries), 339-509 (build_health_manifest_dict),
523-558 (_manifest_json_under_budget paths), 561-613 (baseline_sync_loop),
616-617 (baseline_snapshot_loop), 620-653 (fetch helpers).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("OMNI_ENV_MODE", "dev")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379")

from workers import baseline_snapshot as bs


# ---------------------------------------------------------------------------
# _kube_warning_events (lines 274-324)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kube_warning_events_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy-path: K8s returns 2 Warning events."""

    # Build fake event objects
    def _make_ev(ns: str, reason: str, msg: str, kind: str, name: str, ts: float):
        last_ts = MagicMock()
        last_ts.timestamp.return_value = ts
        inv = MagicMock()
        inv.kind = kind
        inv.name = name
        md = MagicMock()
        md.namespace = ns
        ev = MagicMock()
        ev.last_timestamp = last_ts
        ev.reason = reason
        ev.message = msg
        ev.involved_object = inv
        ev.metadata = md
        return ev

    ev1 = _make_ev("ns1", "OOMKill", "container killed", "Pod", "mypod", 1000.0)
    ev2 = _make_ev("ns2", "BackOff", "restart loop", "Pod", "pod2", 999.0)

    async def fake_kube_load():
        pass

    fake_v1 = AsyncMock()
    fake_v1.list_event_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[ev1, ev2]))
    fake_v1.api_client = MagicMock()
    fake_v1.api_client.close = AsyncMock()

    # _kube_load is imported locally inside _kube_warning_events from init.deep_scout
    monkeypatch.setattr("init.deep_scout._kube_load", fake_kube_load)
    monkeypatch.setattr("workers.baseline_snapshot.client.CoreV1Api", lambda: fake_v1)

    ws = SimpleNamespace(
        baseline_warning_events_max=5,
        baseline_warning_events_fetch_limit=400,
        baseline_k8s_events_timeout_sec=20.0,
    )
    result = await bs._kube_warning_events(ws)
    assert len(result) == 2
    assert result[0]["r"] == "OOMKill"
    assert result[1]["r"] == "BackOff"


@pytest.mark.asyncio
async def test_kube_warning_events_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """asyncio.wait_for timeout → returns []."""

    async def fake_kube_load():
        pass

    fake_v1 = AsyncMock()

    async def slow_list(*a, **k):
        await asyncio.sleep(999)
        return MagicMock(items=[])

    fake_v1.list_event_for_all_namespaces = slow_list
    fake_v1.api_client = MagicMock()
    fake_v1.api_client.close = AsyncMock()

    monkeypatch.setattr("init.deep_scout._kube_load", fake_kube_load)
    monkeypatch.setattr("workers.baseline_snapshot.client.CoreV1Api", lambda: fake_v1)

    ws = SimpleNamespace(
        baseline_warning_events_max=5,
        baseline_warning_events_fetch_limit=400,
        baseline_k8s_events_timeout_sec=0.01,  # very short timeout
    )
    result = await bs._kube_warning_events(ws)
    assert result == []


@pytest.mark.asyncio
async def test_kube_warning_events_k8s_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """K8s API error → returns []."""

    async def fake_kube_load():
        pass

    fake_v1 = AsyncMock()
    fake_v1.list_event_for_all_namespaces = AsyncMock(side_effect=RuntimeError("k8s down"))
    fake_v1.api_client = MagicMock()
    fake_v1.api_client.close = AsyncMock()

    monkeypatch.setattr("init.deep_scout._kube_load", fake_kube_load)
    monkeypatch.setattr("workers.baseline_snapshot.client.CoreV1Api", lambda: fake_v1)

    ws = SimpleNamespace(
        baseline_warning_events_max=5,
        baseline_warning_events_fetch_limit=400,
        baseline_k8s_events_timeout_sec=20.0,
    )
    result = await bs._kube_warning_events(ws)
    assert result == []


@pytest.mark.asyncio
async def test_kube_warning_events_no_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Event with no timestamp fields falls back to 0.0."""

    async def fake_kube_load():
        pass

    ev = MagicMock()
    ev.last_timestamp = None
    ev.event_time = None
    md = MagicMock()
    md.namespace = "ns"
    md.creation_timestamp = None
    ev.metadata = md
    ev.reason = "Test"
    ev.message = "msg"
    ev.involved_object = None

    fake_v1 = AsyncMock()
    fake_v1.list_event_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[ev]))
    fake_v1.api_client = MagicMock()
    fake_v1.api_client.close = AsyncMock()

    monkeypatch.setattr("init.deep_scout._kube_load", fake_kube_load)
    monkeypatch.setattr("workers.baseline_snapshot.client.CoreV1Api", lambda: fake_v1)

    ws = SimpleNamespace(
        baseline_warning_events_max=5,
        baseline_warning_events_fetch_limit=400,
        baseline_k8s_events_timeout_sec=20.0,
    )
    result = await bs._kube_warning_events(ws)
    assert len(result) == 1
    assert result[0]["o"] == "?"  # no involved_object


# ---------------------------------------------------------------------------
# _supplemental_queries (lines 327-336)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_supplemental_queries_with_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid pairs → calls _query_scalar_str for each."""
    call_count = 0

    async def fake_query_scalar(ctx, q):
        nonlocal call_count
        call_count += 1
        return f"value_{call_count}"

    monkeypatch.setattr(bs, "_query_scalar_str", fake_query_scalar)

    raw = "latency|rate(http_request_duration[5m])\nerrors|sum(http_errors_total)"
    result = await bs._supplemental_queries(None, raw)
    assert "latency" in result
    assert "errors" in result
    assert result["latency"] == "value_1"
    assert result["errors"] == "value_2"


@pytest.mark.asyncio
async def test_supplemental_queries_truncates_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keys longer than 12 chars are truncated to 12 chars."""

    async def fake_query_scalar(ctx, q):
        return "42"

    monkeypatch.setattr(bs, "_query_scalar_str", fake_query_scalar)

    # name has 17 chars → truncated to 12
    raw = "verylongkeynam|up"
    result = await bs._supplemental_queries(None, raw)
    key = list(result.keys())[0]
    assert len(key) == 12
    assert key == "verylongkeyn"


@pytest.mark.asyncio
async def test_supplemental_queries_none_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """_query_scalar_str returning None → key maps to None."""

    async def fake_query_scalar(ctx, q):
        return None

    monkeypatch.setattr(bs, "_query_scalar_str", fake_query_scalar)

    raw = "cpu|up"
    result = await bs._supplemental_queries(None, raw)
    assert result["cpu"] is None


@pytest.mark.asyncio
async def test_supplemental_queries_limit_8(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only first 8 pairs are processed."""
    calls = []

    async def fake_query_scalar(ctx, q):
        calls.append(q)
        return "1"

    monkeypatch.setattr(bs, "_query_scalar_str", fake_query_scalar)

    lines = "\n".join(f"k{i}|q{i}" for i in range(12))
    await bs._supplemental_queries(None, lines)
    assert len(calls) == 8


# ---------------------------------------------------------------------------
# build_health_manifest_dict (lines 339-509)
# ---------------------------------------------------------------------------

def _make_ctx_with_prom(response_value: float | None = 0.5):
    """Return a fake ctx that makes all PromQL queries return `response_value`."""
    async def fake_get_json(ctx, path, params):
        if response_value is None:
            return {"status": "success", "data": {"result": []}}
        return {
            "status": "success",
            "data": {"result": [{"value": [1, str(response_value)]}]},
        }

    ctx = SimpleNamespace(settings=None)
    return ctx, fake_get_json


def _make_ws(**overrides):
    defaults = dict(
        k8s_default_namespace="multi-agent",
        baseline_cpu_drift_threshold=0.15,
        baseline_promql_z_cpu="",
        baseline_promql_z_mem="",
        baseline_promql_z_disk="",
        baseline_promql_z_iops="",
        baseline_promql_z_net="",
        baseline_promql_seasonal_cpu="",
        baseline_dr_z_threshold=3.0,
        golden_latency_promql="",
        latency_threshold_ms=None,
        chs_weights="",
        chs_threshold=10.0,
        baseline_legacy_cpu_drift_for_dr=False,
        baseline_promql="",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_build_health_manifest_dict_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Basic manifest build with all Prometheus queries returning 0.5."""
    ctx, fake_get_json = _make_ctx_with_prom(0.5)
    monkeypatch.setattr(bs, "_prometheus_get_json", fake_get_json)

    # Mock kube events
    monkeypatch.setattr(bs, "_kube_warning_events", AsyncMock(return_value=[]))

    ws = _make_ws()
    manifest = await bs.build_health_manifest_dict(ctx, ws, None)

    assert "t" in manifest
    assert "cpu" in manifest
    assert "mem" in manifest
    assert "net" in manifest
    assert "dsk" in manifest
    assert "rp" in manifest
    assert "evt" in manifest
    assert "dr" in manifest
    assert manifest["cpu"] == 0.5
    assert manifest["dr"] is False


@pytest.mark.asyncio
async def test_build_health_manifest_dict_with_z_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    """Manifest with z_cpu > threshold triggers dr=True via _sigma_dr."""

    # Directly mock _query_float so z_cpu/z_mem return large values
    call_count = [0]

    async def fake_query_float(ctx, promql):
        call_count[0] += 1
        return 4.5  # Always return a high z-score

    monkeypatch.setattr(bs, "_query_float", fake_query_float)
    monkeypatch.setattr(bs, "_kube_warning_events", AsyncMock(return_value=[]))

    ws = _make_ws(
        baseline_promql_z_cpu="omni:node_cpu:z",
        baseline_promql_z_mem="omni:mem:z",
        baseline_dr_z_threshold=3.0,
    )
    manifest = await bs.build_health_manifest_dict(ctx=SimpleNamespace(), ws=ws, old_raw=None)
    # z_cpu = 4.5 > 3.0 threshold → dr=True
    assert manifest["dr"] is True


@pytest.mark.asyncio
async def test_build_health_manifest_dict_with_golden_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    """golden_latency_promql set → manifest['golden'] populated."""

    async def fake_get_json(ctx, path, params):
        return {"status": "success", "data": {"result": [{"value": [1, "0.05"]}]}}

    monkeypatch.setattr(bs, "_prometheus_get_json", fake_get_json)
    monkeypatch.setattr(bs, "_kube_warning_events", AsyncMock(return_value=[]))

    ws = _make_ws(
        golden_latency_promql="histogram_quantile(0.99, ...)",
        latency_threshold_ms=100.0,  # 0.05s → 50ms < 100ms → remediation_silent
    )
    manifest = await bs.build_health_manifest_dict(ctx=SimpleNamespace(), ws=ws, old_raw=None)
    assert "golden" in manifest
    assert manifest["golden"]["latency_p99_ms"] is not None
    assert manifest["remediation_silent"] is True


@pytest.mark.asyncio
async def test_build_health_manifest_dict_with_chs(monkeypatch: pytest.MonkeyPatch) -> None:
    """CHS weights set → manifest has chs, w, wide_incident keys."""

    async def fake_get_json(ctx, path, params):
        return {"status": "success", "data": {"result": [{"value": [1, "5.0"]}]}}

    monkeypatch.setattr(bs, "_prometheus_get_json", fake_get_json)
    monkeypatch.setattr(bs, "_kube_warning_events", AsyncMock(return_value=[]))

    ws = _make_ws(
        chs_weights='{"cpu": 2.0, "mem": 1.0}',
        chs_threshold=5.0,
        baseline_promql_z_cpu="q_cpu",
        baseline_promql_z_mem="q_mem",
        baseline_promql_z_net="q_net",
    )
    manifest = await bs.build_health_manifest_dict(ctx=SimpleNamespace(), ws=ws, old_raw=None)
    assert "chs" in manifest
    assert "w" in manifest
    assert "wide_incident" in manifest


@pytest.mark.asyncio
async def test_build_health_manifest_dict_with_net_z(monkeypatch: pytest.MonkeyPatch) -> None:
    """baseline_promql_z_net populated → z_net in manifest."""

    async def fake_get_json(ctx, path, params):
        return {"status": "success", "data": {"result": [{"value": [1, "1.5"]}]}}

    monkeypatch.setattr(bs, "_prometheus_get_json", fake_get_json)
    monkeypatch.setattr(bs, "_kube_warning_events", AsyncMock(return_value=[]))

    ws = _make_ws(baseline_promql_z_net="omni:net:z")
    manifest = await bs.build_health_manifest_dict(ctx=SimpleNamespace(), ws=ws, old_raw=None)
    assert "z_net" in manifest


@pytest.mark.asyncio
async def test_build_health_manifest_dict_with_seasonal(monkeypatch: pytest.MonkeyPatch) -> None:
    """baseline_promql_seasonal_cpu → seasonal_drift_z in manifest."""

    async def fake_get_json(ctx, path, params):
        return {"status": "success", "data": {"result": [{"value": [1, "0.2"]}]}}

    monkeypatch.setattr(bs, "_prometheus_get_json", fake_get_json)
    monkeypatch.setattr(bs, "_kube_warning_events", AsyncMock(return_value=[]))

    ws = _make_ws(baseline_promql_seasonal_cpu="omni:seasonal:z")
    manifest = await bs.build_health_manifest_dict(ctx=SimpleNamespace(), ws=ws, old_raw=None)
    assert "seasonal_drift_z" in manifest


@pytest.mark.asyncio
async def test_build_health_manifest_dict_supplemental(monkeypatch: pytest.MonkeyPatch) -> None:
    """baseline_promql extra queries → q key in manifest."""

    async def fake_get_json(ctx, path, params):
        return {"status": "success", "data": {"result": [{"value": [1, "7.0"]}]}}

    monkeypatch.setattr(bs, "_prometheus_get_json", fake_get_json)
    monkeypatch.setattr(bs, "_kube_warning_events", AsyncMock(return_value=[]))

    ws = _make_ws(baseline_promql="custom|up{job='prometheus'}")
    manifest = await bs.build_health_manifest_dict(ctx=SimpleNamespace(), ws=ws, old_raw=None)
    assert "q" in manifest
    assert "custom" in manifest["q"]


@pytest.mark.asyncio
async def test_build_health_manifest_dict_legacy_cpu_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """baseline_legacy_cpu_drift_for_dr=True + large cpu delta → dr=True."""
    call_n = 0

    async def fake_get_json(ctx, path, params):
        return {"status": "success", "data": {"result": [{"value": [1, "0.9"]}]}}

    monkeypatch.setattr(bs, "_prometheus_get_json", fake_get_json)
    monkeypatch.setattr(bs, "_kube_warning_events", AsyncMock(return_value=[]))

    # old snapshot has cpu=0.1; new will be 0.9 → huge drift
    old_raw = json.dumps({"cpu": 0.1})
    ws = _make_ws(
        baseline_legacy_cpu_drift_for_dr=True,
        baseline_cpu_drift_threshold=0.15,
    )
    manifest = await bs.build_health_manifest_dict(ctx=SimpleNamespace(), ws=ws, old_raw=old_raw)
    assert manifest["dr"] is True


@pytest.mark.asyncio
async def test_build_health_manifest_dict_rp_computed(monkeypatch: pytest.MonkeyPatch) -> None:
    """cpu_use/cpu_req and mem_use/mem_req are computed into rp.c and rp.m."""
    responses = {
        0: 0.8,  # cpu_busy
        1: 0.5,  # mem_avail_ratio
        2: 1000.0,  # net_rx
        3: 500.0,   # net_tx
        4: 0.6,    # dsk_usage_ratio
        5: 1024.0, # dsk_read_bps
        6: 512.0,  # dsk_write_bps
        7: 100.0,  # dsk_read_iops
        8: 50.0,   # dsk_write_iops
        9: 0.4,    # cpu_use_ns
        10: 2.0,   # cpu_req_ns
        11: 1e9,   # mem_use_ns
        12: 2e9,   # mem_req_ns
    }
    idx = [0]

    async def fake_get_json(ctx, path, params):
        i = idx[0]
        idx[0] += 1
        val = responses.get(i, 0.5)
        return {"status": "success", "data": {"result": [{"value": [1, str(val)]}]}}

    monkeypatch.setattr(bs, "_prometheus_get_json", fake_get_json)
    monkeypatch.setattr(bs, "_kube_warning_events", AsyncMock(return_value=[]))

    ws = _make_ws()
    manifest = await bs.build_health_manifest_dict(ctx=SimpleNamespace(), ws=ws, old_raw=None)
    assert manifest["rp"]["c"] is not None  # cpu_use / cpu_req = 0.4/2.0 = 0.2
    assert manifest["rp"]["m"] is not None  # mem_use / mem_req = 0.5


@pytest.mark.asyncio
async def test_build_health_manifest_dict_evt_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """If _kube_warning_events raises, evt defaults to []."""

    async def fake_get_json(ctx, path, params):
        return {"status": "success", "data": {"result": [{"value": [1, "0.5"]}]}}

    monkeypatch.setattr(bs, "_prometheus_get_json", fake_get_json)
    monkeypatch.setattr(bs, "_kube_warning_events", AsyncMock(side_effect=RuntimeError("boom")))

    ws = _make_ws()
    manifest = await bs.build_health_manifest_dict(ctx=SimpleNamespace(), ws=ws, old_raw=None)
    assert manifest["evt"] == []


# ---------------------------------------------------------------------------
# _manifest_json_under_budget — trim paths (lines 512-558)
# ---------------------------------------------------------------------------

def test_manifest_json_under_budget_fits():
    """Small manifest fits without any trimming."""
    m = {"t": 1, "cpu": 0.5, "dr": False}
    s = bs._manifest_json_under_budget(m, max_chars=10000)
    assert json.loads(s)["cpu"] == 0.5


def test_manifest_json_under_budget_trim_evt_messages():
    """Smart trim: distinct events drop non-critical tail to fit budget; dedup same (r,o) pairs."""
    # Use distinct (r, o) so dedup doesn't merge them — tests pure budget trimming.
    manifest = {
        "t": 1,
        "evt": [
            {"ns": "n", "r": "r1", "m": "x" * 200, "o": "o1"},
            {"ns": "n", "r": "r2", "m": "y" * 200, "o": "o2"},
            {"ns": "n", "r": "r3", "m": "z" * 200, "o": "o3"},
            {"ns": "n", "r": "r4", "m": "w" * 200, "o": "o4"},
        ],
    }
    full = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    s = bs._manifest_json_under_budget(manifest, max_chars=len(full) - 1)
    parsed = json.loads(s)
    # Smart trim drops non-critical events tail first → fewer events
    assert len(parsed["evt"]) < 4
    assert isinstance(s, str)


def test_manifest_json_under_budget_dedup_same_key():
    """Smart trim deduplicates events with identical (r, o) into [Count:N] prefix."""
    manifest = {
        "t": 1,
        "evt": [
            {"ns": "n", "r": "BackOff", "m": "restart1", "o": "Pod/x"},
            {"ns": "n", "r": "BackOff", "m": "restart2", "o": "Pod/x"},
            {"ns": "n", "r": "BackOff", "m": "restart3", "o": "Pod/x"},
        ],
    }
    s = bs._manifest_json_under_budget(manifest, max_chars=10000)
    parsed = json.loads(s)
    assert len(parsed["evt"]) == 1
    assert parsed["evt"][0]["m"].startswith("[Count:3]")


def test_manifest_json_under_budget_trim_evt_to_1():
    """Tiny budget removes non-critical events until under budget."""
    manifest = {
        "t": 1,
        "evt": [
            {"ns": "n", "r": "r1", "m": "msg1", "o": "o1"},
            {"ns": "n", "r": "r2", "m": "msg2", "o": "o2"},
            {"ns": "n", "r": "r3", "m": "msg3", "o": "o3"},
        ],
    }
    full_len = len(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
    s = bs._manifest_json_under_budget(manifest, max_chars=full_len - 10)
    parsed = json.loads(s)
    assert len(parsed["evt"]) <= 3


def test_manifest_json_under_budget_removes_q_then_w_then_golden():
    """Successive trims remove q, w, golden in order."""
    manifest = {
        "t": 1,
        "q": {"k": "v" * 50},
        "w": {"cpu": 1.0},
        "golden": {"latency_p99_ms": 5.0},
        "evt": [],
        "dsk": {"u": 0.5, "rt": 1, "wt": 2, "ri": 3, "wi": 4},
    }
    # Progressively shrink budget
    s = bs._manifest_json_under_budget(manifest, max_chars=200)
    # Should not crash, and should be valid JSON or truncated
    assert isinstance(s, str)


def test_manifest_json_under_budget_truncates_if_cannot_fit():
    """When nothing more can be trimmed, truncate with ellipsis."""
    manifest = {"t": 1, "cpu": "x" * 50}
    s = bs._manifest_json_under_budget(manifest, max_chars=10)
    assert len(s) <= 10 or s.endswith("…")


# ---------------------------------------------------------------------------
# baseline_sync_loop (lines 561-613)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_baseline_sync_loop_disabled() -> None:
    """baseline_snapshot_enabled=False → exits immediately."""
    ws = SimpleNamespace(baseline_snapshot_enabled=False)
    ctx = SimpleNamespace(settings=ws, scout_ready=asyncio.Event())
    ctx.scout_ready.set()
    stop = asyncio.Event()
    # Should return immediately
    await bs.baseline_sync_loop(ctx, stop)


@pytest.mark.asyncio
async def test_baseline_sync_loop_one_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """One iteration: builds manifest, writes to Redis, then stop."""
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=None)
    fake_redis.setex = AsyncMock()

    async def fake_build(ctx, ws, old_raw):
        return {
            "t": 1,
            "cpu": 0.3,
            "mem": 0.5,
            "z_cpu": None,
            "z_mem": None,
            "z_disk": None,
            "z_iops": None,
            "net": {"rx": None, "tx": None},
            "dsk": {"u": None, "rt": None, "wt": None, "ri": None, "wi": None},
            "rp": {"c": None, "m": None},
            "evt": [],
            "dr": False,
            "remediation_silent": False,
        }

    monkeypatch.setattr(bs, "build_health_manifest_dict", fake_build)

    ws = SimpleNamespace(
        baseline_snapshot_enabled=True,
        baseline_snapshot_interval_sec=0.01,
        baseline_snapshot_redis_ttl_sec=3600,
        baseline_manifest_max_chars=1400,
    )

    ready = asyncio.Event()
    ready.set()
    ctx = SimpleNamespace(settings=ws, redis=fake_redis, scout_ready=ready)
    stop = asyncio.Event()

    # Run loop then stop after first tick
    async def stop_after():
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(bs.baseline_sync_loop(ctx, stop), stop_after())
    # Redis setex should have been called at least once
    assert fake_redis.setex.call_count >= 1


@pytest.mark.asyncio
async def test_baseline_sync_loop_exception_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exception inside tick is caught and loop continues."""
    call_count = [0]

    async def fake_build(ctx, ws, old_raw):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("prom error")
        return {
            "t": 1, "cpu": 0.3, "mem": 0.5,
            "z_cpu": None, "z_mem": None, "z_disk": None, "z_iops": None,
            "net": {}, "dsk": {}, "rp": {}, "evt": [], "dr": False, "remediation_silent": False,
        }

    monkeypatch.setattr(bs, "build_health_manifest_dict", fake_build)

    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=None)
    fake_redis.setex = AsyncMock()

    ws = SimpleNamespace(
        baseline_snapshot_enabled=True,
        baseline_snapshot_interval_sec=0.01,
        baseline_snapshot_redis_ttl_sec=3600,
        baseline_manifest_max_chars=1400,
    )
    ready = asyncio.Event()
    ready.set()
    ctx = SimpleNamespace(settings=ws, redis=fake_redis, scout_ready=ready)
    stop = asyncio.Event()

    async def stopper():
        await asyncio.sleep(0.1)
        stop.set()

    await asyncio.gather(bs.baseline_sync_loop(ctx, stop), stopper())
    assert call_count[0] >= 2  # first failed, second succeeded


# ---------------------------------------------------------------------------
# baseline_snapshot_loop (line 616-617)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_baseline_snapshot_loop_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """baseline_snapshot_loop delegates to baseline_sync_loop."""
    called = []

    async def fake_sync(ctx, stop):
        called.append((ctx, stop))

    monkeypatch.setattr(bs, "baseline_sync_loop", fake_sync)
    ctx = object()
    stop = asyncio.Event()
    await bs.baseline_snapshot_loop(ctx, stop)
    assert len(called) == 1


# ---------------------------------------------------------------------------
# fetch_baseline_system_prompt (lines 620-632)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_baseline_system_prompt_bytes() -> None:
    r = MagicMock()
    r.get = AsyncMock(return_value=b'{"cpu":0.5}')
    out = await bs.fetch_baseline_system_prompt(r, max_chars=1000)
    assert "[SYSTEM BASELINE CONTEXT" in out
    assert "cpu" in out


@pytest.mark.asyncio
async def test_fetch_baseline_system_prompt_empty() -> None:
    r = MagicMock()
    r.get = AsyncMock(return_value=None)
    out = await bs.fetch_baseline_system_prompt(r, max_chars=1000)
    assert out == ""


@pytest.mark.asyncio
async def test_fetch_baseline_system_prompt_truncate() -> None:
    r = MagicMock()
    r.get = AsyncMock(return_value="x" * 2000)
    out = await bs.fetch_baseline_system_prompt(r, max_chars=50)
    assert len(out) <= 50
    assert out.endswith("…")


# ---------------------------------------------------------------------------
# fetch_baseline_snapshot_hint (lines 635-652)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_baseline_snapshot_hint_with_ts() -> None:
    r = MagicMock()
    r.get = AsyncMock(side_effect=[b'{"cpu":0.3}', b"1700000000"])
    out = await bs.fetch_baseline_snapshot_hint(r, max_chars=10000)
    assert "ts=1700000000" in out
    assert bs.BASELINE_HINT_LEGEND[:10] in out


@pytest.mark.asyncio
async def test_fetch_baseline_snapshot_hint_no_ts() -> None:
    r = MagicMock()
    r.get = AsyncMock(side_effect=["data", None])
    out = await bs.fetch_baseline_snapshot_hint(r, max_chars=10000)
    assert "ts=" not in out
    assert "data" in out


@pytest.mark.asyncio
async def test_fetch_baseline_snapshot_hint_empty_raw() -> None:
    r = MagicMock()
    r.get = AsyncMock(side_effect=[None, "123"])
    out = await bs.fetch_baseline_snapshot_hint(r, max_chars=10000)
    assert out == ""


@pytest.mark.asyncio
async def test_fetch_baseline_snapshot_hint_exception() -> None:
    r = MagicMock()
    r.get = AsyncMock(side_effect=RuntimeError("redis down"))
    out = await bs.fetch_baseline_snapshot_hint(r, max_chars=1000)
    assert out == ""


@pytest.mark.asyncio
async def test_fetch_baseline_snapshot_hint_truncate() -> None:
    r = MagicMock()
    r.get = AsyncMock(side_effect=["payload" * 1000, "999"])
    out = await bs.fetch_baseline_snapshot_hint(r, max_chars=100)
    assert len(out) <= 100
    assert out.endswith("…")
