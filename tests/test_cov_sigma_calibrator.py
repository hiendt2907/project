"""Coverage tests for anomaly/sigma_calibrator.py."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fakeredis.aioredis import FakeRedis


def _ctx(redis=None):
    return SimpleNamespace(redis=redis or FakeRedis(decode_responses=True))


def _mock_df(values: list[float]):
    """Minimal DataFrame-like mock with len() and df['y'] support."""
    series = MagicMock()
    series.astype.return_value = series
    series.dropna.return_value = series
    series.tolist.return_value = values
    df = MagicMock()
    df.__len__ = MagicMock(return_value=len(values))
    df.__getitem__ = MagicMock(return_value=series)
    return df


def _patch_imports(df):
    """Patch the lazy imports inside calibrate_sigma_for_workload."""
    return (
        patch("metrics.prometheus_dataframe.fetch_range_dataframe", new=AsyncMock(return_value=df)),
        patch("workers.sdk_service_tools._duration_to_vm_window", return_value=("1h", "5m")),
    )


@pytest.mark.asyncio
async def test_no_redis_returns_none():
    from anomaly.sigma_calibrator import calibrate_sigma_for_workload
    result = await calibrate_sigma_for_workload(SimpleNamespace(), namespace="ns", deployment="dep")
    assert result is None


@pytest.mark.asyncio
async def test_prometheus_fetch_fail_returns_none():
    from anomaly.sigma_calibrator import calibrate_sigma_for_workload
    ctx = _ctx()
    with (
        patch("anomaly.sigma_calibrator.fetch_range_dataframe", side_effect=RuntimeError("prom down"), create=True),
        patch("anomaly.sigma_calibrator._duration_to_vm_window", return_value=("1h", "5m"), create=True),
    ):
        result = await calibrate_sigma_for_workload(ctx, namespace="ns", deployment="dep")
    assert result is None


@pytest.mark.asyncio
async def test_insufficient_data_returns_none():
    from anomaly.sigma_calibrator import calibrate_sigma_for_workload
    ctx = _ctx()
    df = _mock_df([1.0, 2.0, 3.0])  # < 10 points
    p1, p2 = _patch_imports(df)
    with p1, p2:
        result = await calibrate_sigma_for_workload(ctx, namespace="ns", deployment="dep")
    assert result is None


@pytest.mark.asyncio
async def test_calibrates_stable_workload():
    from anomaly.sigma_calibrator import calibrate_sigma_for_workload
    ctx = _ctx()
    stable = [10.0 + i * 0.01 for i in range(20)]  # low CV
    df = _mock_df(stable)
    p1, p2 = _patch_imports(df)
    with p1, p2:
        result = await calibrate_sigma_for_workload(ctx, namespace="prod", deployment="api")
    assert result is not None
    assert result["threshold"] >= 2.5
    assert result["window"] >= 50
    assert "cv" in result
    assert result["namespace"] == "prod"
    assert result["deployment"] == "api"


@pytest.mark.asyncio
async def test_calibrates_bursty_workload():
    from anomaly.sigma_calibrator import calibrate_sigma_for_workload
    ctx = _ctx()
    bursty = [1.0, 100.0] * 10  # very high CV
    df = _mock_df(bursty)
    p1, p2 = _patch_imports(df)
    with p1, p2:
        result = await calibrate_sigma_for_workload(ctx, namespace="prod", deployment="batch")
    assert result is not None
    assert result["threshold"] > 3.0


@pytest.mark.asyncio
async def test_zero_mean_returns_none():
    from anomaly.sigma_calibrator import calibrate_sigma_for_workload
    ctx = _ctx()
    df = _mock_df([0.0] * 15)
    p1, p2 = _patch_imports(df)
    with p1, p2:
        result = await calibrate_sigma_for_workload(ctx, namespace="ns", deployment="dep")
    assert result is None


@pytest.mark.asyncio
async def test_calibrate_writes_to_redis():
    from anomaly.sigma_calibrator import calibrate_sigma_for_workload
    r = FakeRedis(decode_responses=True)
    ctx = _ctx(redis=r)
    df = _mock_df([float(i) for i in range(10, 30)])
    p1, p2 = _patch_imports(df)
    with p1, p2:
        result = await calibrate_sigma_for_workload(ctx, namespace="ns", deployment="svc")
    assert result is not None
    stored = await r.hgetall("omni:sigma:config:ns:svc")
    assert stored.get("auto_calibrated") == "true"
    assert "threshold" in stored


@pytest.mark.asyncio
async def test_after_dropna_insufficient():
    """Test the second len<10 check: df has 10+ items but dropna reduces below threshold."""
    from anomaly.sigma_calibrator import calibrate_sigma_for_workload
    ctx = _ctx()
    # Mock a df where len(df)=10 but series.tolist() returns fewer than 10 items
    series = MagicMock()
    series.astype.return_value = series
    series.dropna.return_value = series
    series.tolist.return_value = [1.0, 2.0, 3.0]  # only 3 after dropna
    df = MagicMock()
    df.__len__ = MagicMock(return_value=10)  # passes first check
    df.__getitem__ = MagicMock(return_value=series)
    p1, p2 = _patch_imports(df)
    with p1, p2:
        result = await calibrate_sigma_for_workload(ctx, namespace="ns", deployment="dep")
    assert result is None


@pytest.mark.asyncio
async def test_run_calibration_pass_no_redis():
    from anomaly.sigma_calibrator import run_sigma_calibration_pass
    await run_sigma_calibration_pass(SimpleNamespace())  # no error


@pytest.mark.asyncio
async def test_run_calibration_pass_with_sigma_keys():
    from anomaly.sigma_calibrator import run_sigma_calibration_pass
    r = FakeRedis(decode_responses=True)
    await r.hset("omni:sigma:config:ns:svc", mapping={"threshold": "3.0"})
    ctx = _ctx(redis=r)
    df = _mock_df([float(i) for i in range(10, 30)])
    p1, p2 = _patch_imports(df)
    with p1, p2:
        await run_sigma_calibration_pass(ctx)  # no error


@pytest.mark.asyncio
async def test_run_calibration_pass_with_cluster_meta():
    from anomaly.sigma_calibrator import run_sigma_calibration_pass
    r = FakeRedis(decode_responses=True)
    # Add a cluster meta key and sigma metric key to exercise scan paths
    await r.hset("omni:cluster:meta:cls-abc", mapping={"namespace": "prod", "member_count": "2"})
    await r.zadd("3sigma:metric:cpu_ns_svc", {"99.5": 99.5})
    ctx = _ctx(redis=r)
    df = _mock_df([float(i) for i in range(10, 30)])
    p1, p2 = _patch_imports(df)
    with p1, p2:
        await run_sigma_calibration_pass(ctx)  # no error, no workloads to calibrate
