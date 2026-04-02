"""SDK service tools (mocked)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from workers.sdk_service_tools import (
    _vm_user_facing_error,
    resolve_promql_for_args,
    tool_forecast_memory_risk_vm,
    tool_metrics_promql_hints,
    tool_promql_instant,
    tool_promql_range,
    tool_query_historical_metrics,
    tool_redis_health,
    tool_system_psutil,
    tool_timeseries_analyze,
    tool_vm_promql_instant,
    tool_vm_promql_range,
)


def test_vm_user_facing_error_connect_hint() -> None:
    msg = _vm_user_facing_error(httpx.ConnectError("refused"))
    assert "[DATA] error" in msg
    assert "ConnectError" in msg
    assert "k8s/monitor" in msg


def test_vm_user_facing_error_timeout_hint() -> None:
    msg = _vm_user_facing_error(httpx.ReadTimeout("timeout"))
    assert "Timeout" in msg or "timeout" in msg.lower()
    assert "monitor" in msg


def test_vm_user_facing_error_403_not_generic() -> None:
    r = MagicMock()
    r.status_code = 403
    e = httpx.HTTPStatusError("forbidden", request=MagicMock(), response=r)
    msg = _vm_user_facing_error(e)
    assert "khong_co_quyen" in msg


@pytest.mark.asyncio
async def test_system_psutil_returns_lines() -> None:
    with patch("workers.sdk_service_tools.asyncio.to_thread", new_callable=AsyncMock) as tt:
        tt.return_value = "CPU: 1%\nRAM: 50%"
        out = await tool_system_psutil(None, {})
        assert "CPU" in out
        tt.assert_awaited()


@pytest.mark.asyncio
async def test_vm_promql_httpx() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.prometheus_url = "http://prometheus:9090"

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {"__name__": "up"}, "value": [1, "1"]}],
        },
    }
    mock_resp.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("workers.sdk_service_tools.httpx.AsyncClient", return_value=mock_client):
        out = await tool_vm_promql_instant(ctx, {"query": "up"})
        assert "[STATUS] business_hit" in out
        assert "up" in out


@pytest.mark.asyncio
async def test_vm_promql_empty_result_contract() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.prometheus_url = "http://prometheus:9090"

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "status": "success",
        "data": {"resultType": "vector", "result": []},
    }
    mock_resp.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("workers.sdk_service_tools.httpx.AsyncClient", return_value=mock_client):
        out = await tool_vm_promql_instant(ctx, {"query": 'up{nonexistent_label="___no_match___"}'})
    assert "[STATUS] empty_result" in out
    assert "không có dữ liệu nào khớp" in out


@pytest.mark.asyncio
async def test_vm_promql_placeholder_rejected() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.prometheus_url = "http://prometheus:9090"

    with patch("workers.sdk_service_tools.httpx.AsyncClient") as mock_ac:
        out = await tool_vm_promql_instant(ctx, {"query": "metric_value > threshold"})
    mock_ac.assert_not_called()
    assert "[STATUS] error" in out
    assert "placeholder" in out.lower()


@pytest.mark.asyncio
async def test_vm_promql_range_parses_matrix() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.prometheus_url = "http://prometheus:9090"

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"job": "redis"},
                    "values": [[1.0, "0.5"], [2.0, "1.5"]],
                }
            ],
        },
    }
    mock_resp.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("workers.sdk_service_tools.httpx.AsyncClient", return_value=mock_client):
        out = await tool_vm_promql_range(
            ctx,
            {"query": "up", "start": "now-1h", "end": "now", "step": "30s"},
        )
    assert "n_points=2" in out
    assert "t=1.0" in out


def test_promql_aliases_same_as_vm_wrappers() -> None:
    assert tool_promql_instant is tool_vm_promql_instant
    assert tool_promql_range is tool_vm_promql_range


def test_resolve_promql_kube_deployment() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.k8s_default_namespace = "multi-agent"
    q, src = resolve_promql_for_args(
        {
            "target_type": "kube_deployment",
            "namespace": "multi-agent",
            "deployment": "omni-worker",
            "intent": "replica_ratio",
        },
        ctx,
    )
    assert "kube_deployment_status_replicas_available" in q
    assert "kube_deployment_spec_replicas" in q
    assert "kube_deployment" in src


def test_resolve_promql_kube_state_deployment_alias() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.k8s_default_namespace = "x"
    q, _src = resolve_promql_for_args(
        {
            "target_type": "kube_state_deployment",
            "namespace": "ns1",
            "deployment_name": "api",
            "intent": "replica_ratio",
        },
        ctx,
    )
    assert "kube_deployment_spec_replicas" in q


def test_resolve_promql_kube_namespace_pending() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.k8s_default_namespace = "x"
    q, _src = resolve_promql_for_args(
        {
            "target_type": "kube_namespace",
            "namespace": "ns1",
            "intent": "pods_pending",
        },
        ctx,
    )
    assert 'phase="Pending"' in q


@pytest.mark.asyncio
async def test_timeseries_analyze_json() -> None:
    out = await tool_timeseries_analyze(
        None,
        {"values": [1, 2, 3, 4], "forecast_steps": 1},
    )
    assert "forecast_linear" in out
    assert "mean" in out


@pytest.mark.asyncio
async def test_metrics_hints_topic() -> None:
    out = await tool_metrics_promql_hints(None, {"topic": "redis"})
    assert "redis" in out.lower()


@pytest.mark.asyncio
async def test_redis_health_sdk() -> None:
    r = MagicMock()
    r.info = AsyncMock(
        side_effect=[
            {"used_memory_human": "1M", "used_memory": 1, "used_memory_rss": 2, "mem_fragmentation_ratio": 1.0},
            {"connected_clients": 3, "blocked_clients": 0},
            {"instantaneous_ops_per_sec": 10, "total_commands_processed": 100},
            {"role": "master"},
        ]
    )
    r.slowlog_get = AsyncMock(return_value=[{"id": 1, "duration": 2, "command": "GET x"}])
    r.execute_command = AsyncMock(return_value="stats_line")
    ctx = MagicMock()
    ctx.redis = r
    out = await tool_redis_health(ctx, {})
    assert "used_memory_human" in out
    assert "slowlog" in out.lower()
    assert "memory_malloc_stats" in out


@pytest.mark.asyncio
async def test_query_historical_metrics_short() -> None:
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

    with patch("workers.sdk_service_tools._prometheus_get_json", new_callable=AsyncMock, return_value=mock_resp):
        out = await tool_query_historical_metrics(
            ctx,
            {"query": "up", "start": "now-1h", "end": "now", "step": "1m"},
        )
    assert "n=2" in out
    assert "min=" in out


@pytest.mark.asyncio
async def test_forecast_memory_risk_vm_total_gib() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.prometheus_url = "http://prometheus:9090"

    range_json = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {},
                    "values": [[float(i * 60), str(1e9 + i * 1e6)] for i in range(20)],
                }
            ],
        },
    }

    with patch("workers.sdk_service_tools._prometheus_get_json", new_callable=AsyncMock, return_value=range_json):
        out = await tool_forecast_memory_risk_vm(
            ctx,
            {
                "query": "node_memory_Active_bytes",
                "total_ram_gib": 16.0,
                "kind": "usage",
            },
        )
    assert "oom_or_pressure_risk" in out or "predicted_last_gib" in out
