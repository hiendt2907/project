"""Expert SDK tools (mocked)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workers.sdk_service_tools import (
    tool_predict_resource_exhaustion,
    tool_query_vm_timeseries,
    tool_redis_expert_check,
)


@pytest.mark.asyncio
async def test_query_vm_timeseries_builds_chart() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.prometheus_url = "http://prometheus:9090"
    ctx.telegram = None

    mock_resp = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {}, "values": [[1000.0, "1"], [1060.0, "2"]]}],
        },
    }

    with patch("workers.sdk_service_tools._prometheus_get_json", new_callable=AsyncMock, return_value=mock_resp), patch(
        "workers.sdk_service_tools.vm_timeseries_to_line_chart_png_bytes", return_value=b"PNG"
    ):
        out = await tool_query_vm_timeseries(ctx, {"query": "up", "duration": "1h"})
    assert "[DATA]" in out
    assert "n_points" in out


@pytest.mark.asyncio
async def test_redis_expert_check() -> None:
    r = MagicMock()
    r.info = AsyncMock(
        return_value={
            "used_memory_human": "1M",
            "used_memory_rss": 2,
            "used_memory_dataset": 1,
            "mem_fragmentation_ratio": 1.2,
        }
    )
    r.slowlog_get = AsyncMock(return_value=[])
    r.config_get = AsyncMock(return_value={"maxmemory": "0"})
    ctx = MagicMock()
    ctx.redis = r
    out = await tool_redis_expert_check(ctx, {})
    assert "[DATA]" in out
    assert "fragmentation" in out.lower() or "mem_fragmentation" in out.lower()


@pytest.mark.asyncio
async def test_predict_resource_exhaustion_increasing() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.prometheus_url = "http://prometheus:9090"

    vals = [float(i) for i in range(30)]
    mock_resp = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {}, "values": [[float(i * 60), str(v)] for i, v in enumerate(vals)]}],
        },
    }

    with patch("workers.sdk_service_tools._prometheus_get_json", new_callable=AsyncMock, return_value=mock_resp):
        out = await tool_predict_resource_exhaustion(
            ctx,
            {"metric_name": "up", "horizon": "6h"},
        )
    assert "[DATA]" in out
    assert "slope" in out.lower() or "hours" in out.lower()
