"""PromQL tự sinh từ intent — không bắt user viết PromQL."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workers.promql_presets import build_promql_from_intent, resolve_intent_from_keywords
from workers.sdk_service_tools import (
    resolve_promql_for_args,
    tool_query_prometheus_metrics,
    tool_query_victoria_metrics,
)


def test_build_promql_cpu_with_pod_regex() -> None:
    q = build_promql_from_intent("cpu", namespace="multi-agent", pod_name="omni-worker")
    assert "container_cpu_usage_seconds_total" in q
    assert "namespace=\"multi-agent\"" in q
    assert "pod=~" in q or 'pod="' in q
    assert re.search(r"omni-worker", q)


def test_build_promql_ram_disk() -> None:
    r = build_promql_from_intent("ram", namespace="ns1", pod_name="p")
    assert "container_memory_working_set_bytes" in r
    d = build_promql_from_intent("disk", namespace="ns1", pod_name="p")
    assert "container_fs_usage_bytes" in d


def test_resolve_intent_from_keywords() -> None:
    assert resolve_intent_from_keywords("kiểm tra RAM pod") == "ram"
    assert resolve_intent_from_keywords("CPU usage") == "cpu"


def test_resolve_promql_for_args_explicit_overrides() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.k8s_default_namespace = "multi-agent"
    q, src = resolve_promql_for_args({"query": "up"}, ctx)
    assert q == "up"
    assert "explicit" in src


def test_resolve_promql_for_args_missing_pod_raises() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.k8s_default_namespace = "multi-agent"
    with pytest.raises(ValueError, match="pod_name"):
        resolve_promql_for_args({"intent": "cpu", "namespace": "multi-agent"}, ctx)


def test_resolve_promql_for_args_auto_cpu() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.k8s_default_namespace = "multi-agent"
    q, src = resolve_promql_for_args(
        {"intent": "cpu", "namespace": "multi-agent", "pod_name": "omni-worker"},
        ctx,
    )
    assert "container_cpu_usage_seconds_total" in q
    assert "auto" in src


@pytest.mark.asyncio
async def test_query_victoria_metrics_missing_pod_raises() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.k8s_default_namespace = "multi-agent"
    with pytest.raises(ValueError, match="pod_name"):
        await tool_query_victoria_metrics(ctx, {"intent": "cpu", "namespace": "multi-agent"})


@pytest.mark.asyncio
async def test_query_victoria_metrics_forecast_chart() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.prometheus_url = "http://prometheus:9090"
    ctx.telegram = None

    mock_resp = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {},
                    "values": [[float(i * 60), str(1.0 + i * 0.01)] for i in range(20)],
                }
            ],
        },
    }

    with patch("workers.sdk_service_tools._prometheus_get_json", new_callable=AsyncMock, return_value=mock_resp):
        out = await tool_query_victoria_metrics(
            ctx,
            {
                "intent": "cpu",
                "namespace": "multi-agent",
                "pod_name": "omni-worker",
                "duration": "1h",
                "forecast": True,
                "forecast_horizon": "1h",
            },
        )
    assert "[DATA]" in out
    assert '"forecast": true' in out or "'forecast': True" in out
    assert "trend_meta" in out


def test_query_prometheus_metrics_alias() -> None:
    assert tool_query_prometheus_metrics is tool_query_victoria_metrics
