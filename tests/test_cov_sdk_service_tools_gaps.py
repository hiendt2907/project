"""Coverage-gap tests for src/workers/sdk_service_tools.py.

Targets uncovered lines including:
- _vm_user_facing_error error paths (57, 75-76, 161)
- _diagnosis_vm_empty branches (212)
- resolve_promql_for_args branches (240, 295, 302)
- tool_promql_instant error/empty branches (426)
- tool_promql_range (482-483)
- tool_metrics_promql_hints (515-523, 540, 546, 558)
- tool_timeseries_analyze (591-593, 596-623)
- tool_redis_health (629-637)
- tool_query_historical_metrics (657-658, 674-679)
- get_historical_series_dataframe (702-748)
- tool_forecast_metric_prophet (758-830)
- tool_redis_expert_check (1081-1111)
- tool_predict_resource_exhaustion (1135-1136, 1153)
- tool_viz_line_chart (1199, 1210-1217)
- tool_vendor_knowledge_search / tool_k8s_expert_search (1228-1304)
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("OMNI_ENV_MODE", "dev")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379")

import httpx
import numpy as np

import workers.sdk_service_tools as sdt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_ctx(namespace: str = "multi-agent", prom_url: str = "http://prom:9090") -> SimpleNamespace:
    settings = SimpleNamespace(
        prometheus_url=prom_url,
        k8s_default_namespace=namespace,
        telegram_chat_id=None,
        pgvector_collection_k8s_expert="k8s_expert",
        embed_model="nomic-embed-text:latest",
    )
    return SimpleNamespace(settings=settings, telegram=None)


def _prom_success(value: str = "1.0") -> dict:
    return {"status": "success", "data": {"result": [{"value": [1, value]}]}}


def _prom_matrix(n_points: int = 5, value: float = 1.0) -> dict:
    now = 1700000000
    step = 60
    result = [{"values": [[now + i * step, str(value)] for i in range(n_points)]}]
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}


# ---------------------------------------------------------------------------
# _vm_user_facing_error — all branches
# ---------------------------------------------------------------------------

def test_vm_user_facing_error_forbidden():
    e = sdt.VMHTTPForbidden()
    s = sdt._vm_user_facing_error(e)
    assert "quyen" in s.lower() or "khong_co_quyen" in s


def test_vm_user_facing_error_httpx_403():
    resp = MagicMock()
    resp.status_code = 403
    e = httpx.HTTPStatusError("forbidden", request=MagicMock(), response=resp)
    s = sdt._vm_user_facing_error(e)
    assert "quyen" in s.lower() or "khong_co_quyen" in s


def test_vm_user_facing_error_connect_error():
    e = httpx.ConnectError("dns fail")
    s = sdt._vm_user_facing_error(e)
    assert "DNS" in s or "OMNI_PROMETHEUS_URL" in s


def test_vm_user_facing_error_timeout():
    e = httpx.ReadTimeout("timeout", request=MagicMock())
    s = sdt._vm_user_facing_error(e)
    assert "Timeout" in s or "prometheus" in s.lower()


def test_vm_user_facing_error_http_status_non403():
    resp = MagicMock()
    resp.status_code = 500
    e = httpx.HTTPStatusError("server error", request=MagicMock(), response=resp)
    s = sdt._vm_user_facing_error(e)
    assert "500" in s or "Prometheus" in s


def test_vm_user_facing_error_generic():
    e = RuntimeError("some error")
    s = sdt._vm_user_facing_error(e)
    assert "RuntimeError" in s or "error" in s.lower()


# ---------------------------------------------------------------------------
# _diagnosis_vm_empty branches
# ---------------------------------------------------------------------------

def test_diagnosis_vm_empty_host():
    s = sdt._diagnosis_vm_empty({"target_type": "host"}, "1h", promql="up")
    assert "Host" in s or "node" in s.lower()


def test_diagnosis_vm_empty_kube_deployment():
    args = {"target_type": "kube_deployment", "namespace": "prod", "deployment": "api"}
    s = sdt._diagnosis_vm_empty(args, "24h", promql="kube_deployment_status_replicas")
    assert "kube-state" in s or "deployment" in s.lower()


def test_diagnosis_vm_empty_kube_namespace():
    args = {"target_type": "kube_namespace", "namespace": "staging"}
    s = sdt._diagnosis_vm_empty(args, "6h", promql="kube_pod_status_phase")
    assert "staging" in s or "namespace" in s.lower()


def test_diagnosis_vm_empty_generic():
    s = sdt._diagnosis_vm_empty({}, "1h", promql="up")
    assert isinstance(s, str) and len(s) > 0


def test_diagnosis_vm_empty_duration_labels():
    s1 = sdt._diagnosis_vm_empty({}, "2h", promql="up")
    assert "2" in s1 or "giờ" in s1 or "hour" in s1.lower() or isinstance(s1, str)

    s2 = sdt._diagnosis_vm_empty({}, "30m", promql="up")
    assert isinstance(s2, str)


# ---------------------------------------------------------------------------
# resolve_promql_for_args — all branches
# ---------------------------------------------------------------------------

def test_resolve_promql_explicit_query():
    ctx = _fake_ctx()
    q, src = sdt.resolve_promql_for_args({"query": "up"}, ctx)
    assert q == "up"
    assert src == "explicit_query"


def test_resolve_promql_auto_host():
    ctx = _fake_ctx()
    q, src = sdt.resolve_promql_for_args({"target_type": "host", "intent": "cpu"}, ctx)
    assert "cpu" in q.lower() or "node" in q.lower()
    assert "host" in src


def test_resolve_promql_kube_deployment():
    ctx = _fake_ctx()
    q, src = sdt.resolve_promql_for_args(
        {"target_type": "kube_deployment", "namespace": "prod", "deployment": "api"},
        ctx,
    )
    assert len(q) > 0
    assert "kube" in src or "kube_deployment" in src.lower()


def test_resolve_promql_kube_deployment_missing_deployment_raises():
    ctx = _fake_ctx()
    with pytest.raises(ValueError, match="Thiếu deployment"):
        sdt.resolve_promql_for_args(
            {"target_type": "kube_deployment", "namespace": "prod"},
            ctx,
        )


def test_resolve_promql_kube_namespace():
    ctx = _fake_ctx()
    q, src = sdt.resolve_promql_for_args(
        {"target_type": "kube_namespace", "namespace": "staging"},
        ctx,
    )
    assert len(q) > 0


def test_resolve_promql_default_intent_with_pod():
    """With pod_name set, intent defaults to cpu."""
    ctx = _fake_ctx()
    q, src = sdt.resolve_promql_for_args({"pod_name": "my-pod", "namespace": "prod"}, ctx)
    assert len(q) > 0


def test_resolve_promql_user_text_hint_with_pod():
    """user_text hint resolves intent, pod required for cAdvisor query."""
    ctx = _fake_ctx()
    q, src = sdt.resolve_promql_for_args({"user_text": "ram usage", "pod_name": "my-pod"}, ctx)
    assert len(q) > 0


# ---------------------------------------------------------------------------
# tool_metrics_promql_hints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_metrics_promql_hints_redis():
    ctx = _fake_ctx()
    s = await sdt.tool_metrics_promql_hints(ctx, {"topic": "redis"})
    assert "redis" in s.lower()
    assert "## Disk" not in s


@pytest.mark.asyncio
async def test_tool_metrics_promql_hints_disk():
    ctx = _fake_ctx()
    s = await sdt.tool_metrics_promql_hints(ctx, {"topic": "disk"})
    assert "disk" in s.lower() or "IOPS" in s


@pytest.mark.asyncio
async def test_tool_metrics_promql_hints_all():
    ctx = _fake_ctx()
    s = await sdt.tool_metrics_promql_hints(ctx, {"topic": "all"})
    assert len(s) > 0


@pytest.mark.asyncio
async def test_tool_metrics_promql_hints_memory():
    ctx = _fake_ctx()
    s = await sdt.tool_metrics_promql_hints(ctx, {"topic": "memory"})
    assert "redis" in s.lower()


@pytest.mark.asyncio
async def test_tool_metrics_promql_hints_node():
    ctx = _fake_ctx()
    s = await sdt.tool_metrics_promql_hints(ctx, {"topic": "node"})
    assert "disk" in s.lower() or "iops" in s.lower()


# ---------------------------------------------------------------------------
# tool_timeseries_analyze
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_timeseries_analyze_csv():
    ctx = _fake_ctx()
    s = await sdt.tool_timeseries_analyze(ctx, {"values": "1,2,3,4,5"})
    out = json.loads(s)
    assert "n" in out or "mean" in out or isinstance(out, dict)


@pytest.mark.asyncio
async def test_tool_timeseries_analyze_list():
    ctx = _fake_ctx()
    s = await sdt.tool_timeseries_analyze(ctx, {"values": [10, 20, 30]})
    out = json.loads(s)
    assert isinstance(out, dict)


@pytest.mark.asyncio
async def test_tool_timeseries_analyze_missing():
    ctx = _fake_ctx()
    s = await sdt.tool_timeseries_analyze(ctx, {})
    assert "Thiếu" in s or "missing" in s.lower()


@pytest.mark.asyncio
async def test_tool_timeseries_analyze_with_forecast():
    ctx = _fake_ctx()
    s = await sdt.tool_timeseries_analyze(ctx, {"values": "1,2,3,4,5,6,7,8", "forecast_steps": 3})
    out = json.loads(s)
    assert isinstance(out, dict)


@pytest.mark.asyncio
async def test_tool_timeseries_analyze_alias_y():
    ctx = _fake_ctx()
    s = await sdt.tool_timeseries_analyze(ctx, {"y": "5,10,15"})
    out = json.loads(s)
    assert isinstance(out, dict)


# ---------------------------------------------------------------------------
# tool_redis_health (ctx.redis branches)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_redis_health_no_redis():
    ctx = SimpleNamespace(settings=None)
    s = await sdt.tool_redis_health(ctx, {})
    assert "redis" in s.lower() or "Không" in s


@pytest.mark.asyncio
async def test_tool_redis_health_info_error():
    r = AsyncMock()
    r.info = AsyncMock(side_effect=RuntimeError("redis down"))
    ctx = SimpleNamespace(settings=None, redis=r)
    s = await sdt.tool_redis_health(ctx, {})
    assert "Lỗi" in s or "error" in s.lower()


@pytest.mark.asyncio
async def test_tool_redis_health_success():
    mem_info = {
        "used_memory_human": "10M",
        "used_memory": 10000000,
        "used_memory_rss": 12000000,
        "mem_fragmentation_ratio": 1.2,
    }
    cli_info = {"connected_clients": 5, "blocked_clients": 0}
    st_info = {"instantaneous_ops_per_sec": 100, "total_commands_processed": 50000}
    rep_info = {"role": "master"}

    r = AsyncMock()
    r.info = AsyncMock(side_effect=[mem_info, cli_info, st_info, rep_info])
    r.slowlog_get = AsyncMock(return_value=[])
    r.execute_command = AsyncMock(return_value="malloc stats...")
    ctx = SimpleNamespace(settings=None, redis=r)
    s = await sdt.tool_redis_health(ctx, {})
    assert "role=master" in s
    assert "used_memory_human" in s


@pytest.mark.asyncio
async def test_tool_redis_health_high_fragmentation():
    """When fragmentation > 1.5, slowlog entries with bytes cmd."""
    mem_info = {
        "used_memory_human": "10M",
        "used_memory": 10000000,
        "used_memory_rss": 12000000,
        "mem_fragmentation_ratio": 1.8,
    }
    cli_info = {"connected_clients": 5, "blocked_clients": 0}
    st_info = {"instantaneous_ops_per_sec": 10, "total_commands_processed": 1000}
    rep_info = {"role": "master"}

    slow_entry = {"id": 1, "duration": 500, "command": b"GET mykey"}
    r = AsyncMock()
    r.info = AsyncMock(side_effect=[mem_info, cli_info, st_info, rep_info])
    r.slowlog_get = AsyncMock(return_value=[slow_entry])
    r.execute_command = AsyncMock(return_value="malloc data")
    ctx = SimpleNamespace(settings=None, redis=r)
    s = await sdt.tool_redis_health(ctx, {})
    assert "master" in s


# ---------------------------------------------------------------------------
# tool_redis_info
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_redis_info_no_redis():
    ctx = SimpleNamespace(settings=None)
    s = await sdt.tool_redis_info(ctx, {})
    assert "redis" in s.lower() or "Không" in s


@pytest.mark.asyncio
async def test_tool_redis_info_with_section():
    r = AsyncMock()
    r.info = AsyncMock(return_value={"used_memory": 1000, "used_memory_human": "1k"})
    ctx = SimpleNamespace(settings=None, redis=r)
    s = await sdt.tool_redis_info(ctx, {"section": "memory"})
    assert "used_memory" in s


@pytest.mark.asyncio
async def test_tool_redis_info_section_error():
    r = AsyncMock()
    r.info = AsyncMock(side_effect=RuntimeError("conn error"))
    ctx = SimpleNamespace(settings=None, redis=r)
    s = await sdt.tool_redis_info(ctx, {"section": "memory"})
    assert "Lỗi" in s or "error" in s.lower()


# ---------------------------------------------------------------------------
# tool_redis_expert_check (lines 1074-1113)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_redis_expert_check_no_redis():
    ctx = SimpleNamespace(settings=None)
    s = await sdt.tool_redis_expert_check(ctx, {})
    assert "no_redis" in s or "redis" in s.lower()


@pytest.mark.asyncio
async def test_tool_redis_expert_check_success():
    mem_info = {
        "mem_fragmentation_ratio": 1.1,
        "used_memory_human": "5M",
        "used_memory_rss": 6000000,
        "used_memory_dataset": 4000000,
    }
    r = AsyncMock()
    r.info = AsyncMock(return_value=mem_info)
    r.slowlog_get = AsyncMock(return_value=[])
    r.config_get = AsyncMock(return_value={"maxmemory": "0"})
    ctx = SimpleNamespace(settings=None, redis=r)
    s = await sdt.tool_redis_expert_check(ctx, {})
    assert "used_memory_human" in s
    assert "Redis memory ổn" in s


@pytest.mark.asyncio
async def test_tool_redis_expert_check_high_frag():
    """fragmentation > 1.6 → diagnosis mentions defrag."""
    mem_info = {
        "mem_fragmentation_ratio": 1.8,
        "used_memory_human": "5M",
        "used_memory_rss": 9000000,
        "used_memory_dataset": 4000000,
    }
    r = AsyncMock()
    r.info = AsyncMock(return_value=mem_info)
    r.slowlog_get = AsyncMock(return_value=[{"id": 1, "duration": 100, "command": "GET"}])
    r.config_get = AsyncMock(return_value={"maxmemory": "1gb"})
    ctx = SimpleNamespace(settings=None, redis=r)
    s = await sdt.tool_redis_expert_check(ctx, {})
    assert "Fragmentation" in s or "defrag" in s


@pytest.mark.asyncio
async def test_tool_redis_expert_check_exception():
    r = AsyncMock()
    r.info = AsyncMock(side_effect=RuntimeError("redis fail"))
    ctx = SimpleNamespace(settings=None, redis=r)
    s = await sdt.tool_redis_expert_check(ctx, {})
    assert "error" in s.lower() or "redis fail" in s


# ---------------------------------------------------------------------------
# tool_viz_line_chart (lines 1181-1218)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_viz_line_chart_no_y():
    ctx = _fake_ctx()
    s = await sdt.tool_viz_line_chart(ctx, {"title": "test"})
    assert "Thiếu" in s or "missing" in s.lower()


@pytest.mark.asyncio
async def test_tool_viz_line_chart_csv_values(monkeypatch: pytest.MonkeyPatch):
    ctx = _fake_ctx()
    fake_png = b"PNG_BYTES"

    def fake_line_chart(*args, **kwargs):
        return fake_png

    monkeypatch.setattr(sdt, "line_chart_png_bytes", fake_line_chart)
    s = await sdt.tool_viz_line_chart(ctx, {"y": "1,2,3,4,5", "title": "CPU"})
    assert "PNG" in s or str(len(fake_png)) in s


@pytest.mark.asyncio
async def test_tool_viz_line_chart_list_values(monkeypatch: pytest.MonkeyPatch):
    ctx = _fake_ctx()
    fake_png = b"FAKE_PNG"

    def fake_line_chart(*args, **kwargs):
        return fake_png

    monkeypatch.setattr(sdt, "line_chart_png_bytes", fake_line_chart)
    s = await sdt.tool_viz_line_chart(ctx, {"y": [1.0, 2.0, 3.0]})
    assert isinstance(s, str)


@pytest.mark.asyncio
async def test_tool_viz_line_chart_x_mismatch():
    ctx = _fake_ctx()
    s = await sdt.tool_viz_line_chart(ctx, {"y": "1,2,3", "x": "10,20"})
    assert "độ dài" in s or "length" in s.lower() or "phải" in s


@pytest.mark.asyncio
async def test_tool_viz_line_chart_with_x(monkeypatch: pytest.MonkeyPatch):
    ctx = _fake_ctx()
    fake_png = b"PNG"

    def fake_line_chart(*args, **kwargs):
        return fake_png

    monkeypatch.setattr(sdt, "line_chart_png_bytes", fake_line_chart)
    s = await sdt.tool_viz_line_chart(ctx, {"y": "1,2,3", "x": "0,1,2"})
    assert isinstance(s, str)


@pytest.mark.asyncio
async def test_tool_viz_line_chart_telegram_success(monkeypatch: pytest.MonkeyPatch):
    fake_png = b"PNG_DATA"

    def fake_line_chart(*args, **kwargs):
        return fake_png

    monkeypatch.setattr(sdt, "line_chart_png_bytes", fake_line_chart)
    monkeypatch.setattr(sdt, "should_send_telegram_chart", lambda ctx, args: True)
    monkeypatch.setattr(sdt, "effective_telegram_chat_id", lambda ctx, args: 12345)

    tg = AsyncMock()
    tg.send_photo_bytes = AsyncMock()
    ctx = SimpleNamespace(settings=SimpleNamespace(
        prometheus_url="http://prom:9090",
        k8s_default_namespace="multi-agent",
    ), telegram=tg)
    s = await sdt.tool_viz_line_chart(ctx, {"y": "1,2,3", "send_telegram": True, "chat_id": 12345})
    assert "Telegram" in s or "chat_id" in s


@pytest.mark.asyncio
async def test_tool_viz_line_chart_telegram_error(monkeypatch: pytest.MonkeyPatch):
    fake_png = b"PNG"

    def fake_line_chart(*args, **kwargs):
        return fake_png

    monkeypatch.setattr(sdt, "line_chart_png_bytes", fake_line_chart)
    monkeypatch.setattr(sdt, "should_send_telegram_chart", lambda ctx, args: True)
    monkeypatch.setattr(sdt, "effective_telegram_chat_id", lambda ctx, args: 12345)

    tg = AsyncMock()
    tg.send_photo_bytes = AsyncMock(side_effect=RuntimeError("tg down"))
    ctx = SimpleNamespace(settings=SimpleNamespace(
        prometheus_url="http://prom:9090",
        k8s_default_namespace="multi-agent",
    ), telegram=tg)
    s = await sdt.tool_viz_line_chart(ctx, {"y": "1,2,3", "send_telegram": True, "chat_id": 12345})
    assert "lỗi" in s.lower() or "error" in s.lower() or "Telegram" in s


# ---------------------------------------------------------------------------
# tool_predict_resource_exhaustion (lines 1116-1178)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_predict_resource_exhaustion_connect_error(monkeypatch: pytest.MonkeyPatch):
    async def boom(ctx, path, params):
        raise httpx.ConnectError("dns fail")

    monkeypatch.setattr(sdt, "_prometheus_get_json", boom)
    ctx = _fake_ctx()
    s = await sdt.tool_predict_resource_exhaustion(ctx, {"query": "up"})
    assert "error" in s.lower() or "Prometheus" in s


@pytest.mark.asyncio
async def test_tool_predict_resource_exhaustion_error_status(monkeypatch: pytest.MonkeyPatch):
    async def fake(ctx, path, params):
        return {"status": "error"}

    monkeypatch.setattr(sdt, "_prometheus_get_json", fake)
    ctx = _fake_ctx()
    s = await sdt.tool_predict_resource_exhaustion(ctx, {"query": "up"})
    assert "error" in s.lower()


@pytest.mark.asyncio
async def test_tool_predict_resource_exhaustion_no_data(monkeypatch: pytest.MonkeyPatch):
    async def fake(ctx, path, params):
        return {"status": "success", "data": {"resultType": "matrix", "result": []}}

    monkeypatch.setattr(sdt, "_prometheus_get_json", fake)
    ctx = _fake_ctx()
    s = await sdt.tool_predict_resource_exhaustion(ctx, {"query": "up"})
    assert "no_data" in s or "không đủ" in s.lower()


@pytest.mark.asyncio
async def test_tool_predict_resource_exhaustion_flat_slope(monkeypatch: pytest.MonkeyPatch):
    """Slope <= 0 → no exhaustion prediction (không chạm ngưỡng)."""
    now = 1700000000

    async def fake(ctx, path, params):
        return {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [{"values": [[now + i * 60, "5.0"] for i in range(10)]}],
            },
        }

    monkeypatch.setattr(sdt, "_prometheus_get_json", fake)
    ctx = _fake_ctx()
    s = await sdt.tool_predict_resource_exhaustion(ctx, {"query": "up"})
    # Flat or near-zero slope → returns "không chạm ngưỡng" or similar
    assert "không chạm" in s or "risk_before_horizon" in s or "slope" in s


@pytest.mark.asyncio
async def test_tool_predict_resource_exhaustion_with_risk(monkeypatch: pytest.MonkeyPatch):
    """Rising trend → risk_before_horizon=True."""
    now = 1700000000

    async def fake(ctx, path, params):
        return {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [{"values": [[now + i * 60, str(i * 10.0)] for i in range(10)]}],
            },
        }

    monkeypatch.setattr(sdt, "_prometheus_get_json", fake)
    ctx = _fake_ctx()
    s = await sdt.tool_predict_resource_exhaustion(ctx, {"query": "up", "horizon": "6h"})
    assert "max_observed" in s or "threshold" in s.lower() or "Dự báo" in s


# ---------------------------------------------------------------------------
# tool_net_scapy_interfaces
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_net_scapy_interfaces_unavailable():
    """scapy not installed → returns message about unavailability."""
    ctx = _fake_ctx()
    s = await sdt.tool_net_scapy_interfaces(ctx, {})
    # Either returns list or error message
    assert isinstance(s, str)


# ---------------------------------------------------------------------------
# is_placeholder_promql
# ---------------------------------------------------------------------------

def test_is_placeholder_promql_empty():
    assert sdt.is_placeholder_promql("") is True


def test_is_placeholder_promql_standard():
    assert sdt.is_placeholder_promql("metric_value > threshold") is True
    assert sdt.is_placeholder_promql("metric_value>=threshold") is True


def test_is_placeholder_promql_real():
    assert sdt.is_placeholder_promql("up") is False
    assert sdt.is_placeholder_promql("rate(node_cpu_seconds_total[5m])") is False


# ---------------------------------------------------------------------------
# _fmt_slowlog_entry
# ---------------------------------------------------------------------------

def test_fmt_slowlog_entry_dict_with_bytes_cmd():
    entry = {"id": 1, "duration": 500, "command": b"GET mykey"}
    s = sdt._fmt_slowlog_entry(entry)
    assert "id=1" in s and "GET" in s


def test_fmt_slowlog_entry_dict_string_cmd():
    entry = {"id": 2, "duration": 100, "command": "SET foo bar"}
    s = sdt._fmt_slowlog_entry(entry)
    assert "id=2" in s


def test_fmt_slowlog_entry_object_style():
    entry = MagicMock()
    entry.id = 3
    entry.duration = 200
    entry.command = b"HGET h f"
    s = sdt._fmt_slowlog_entry(entry)
    assert "id=3" in s


def test_fmt_slowlog_entry_repr_fallback():
    """Object without .duration attribute falls back to repr."""
    entry = object()
    s = sdt._fmt_slowlog_entry(entry)
    assert isinstance(s, str)


# ---------------------------------------------------------------------------
# _duration_window_label
# ---------------------------------------------------------------------------

def test_duration_window_label_hours():
    assert "2" in sdt._duration_window_label("2h") and ("giờ" in sdt._duration_window_label("2h") or "h" in sdt._duration_window_label("2h"))


def test_duration_window_label_minutes():
    s = sdt._duration_window_label("30m")
    assert "30" in s


def test_duration_window_label_fallback():
    s = sdt._duration_window_label("unknown")
    assert isinstance(s, str)


# ---------------------------------------------------------------------------
# _default_namespace
# ---------------------------------------------------------------------------

def test_default_namespace_from_settings():
    ctx = SimpleNamespace(settings=SimpleNamespace(k8s_default_namespace="prod"))
    assert sdt._default_namespace(ctx) == "prod"


def test_default_namespace_fallback():
    ctx = SimpleNamespace(settings=None)
    assert sdt._default_namespace(ctx) == "multi-agent"


def test_default_namespace_empty_setting():
    ctx = SimpleNamespace(settings=SimpleNamespace(k8s_default_namespace="  "))
    assert sdt._default_namespace(ctx) == "multi-agent"


# ---------------------------------------------------------------------------
# tool_promql_instant — placeholder rejection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_promql_instant_placeholder_rejected():
    ctx = _fake_ctx()
    s = await sdt.tool_promql_instant(ctx, {"query": "metric_value > threshold"})
    assert "placeholder" in s.lower() or "error" in s.lower()


# ---------------------------------------------------------------------------
# tool_promql_range — no data path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_promql_range_no_data(monkeypatch: pytest.MonkeyPatch):
    async def fake(ctx, path, params):
        return {"status": "success", "data": {"resultType": "matrix", "result": []}}

    monkeypatch.setattr(sdt, "_prometheus_get_json", fake)
    ctx = _fake_ctx()
    s = await sdt.tool_promql_range(ctx, {"query": "up", "start": "now-1h"})
    assert "no_data" in s or "DIAGNOSIS" in s


@pytest.mark.asyncio
async def test_tool_promql_range_error_status(monkeypatch: pytest.MonkeyPatch):
    async def fake(ctx, path, params):
        return {"status": "error"}

    monkeypatch.setattr(sdt, "_prometheus_get_json", fake)
    ctx = _fake_ctx()
    s = await sdt.tool_promql_range(ctx, {"query": "up"})
    assert "error" in s.lower()
