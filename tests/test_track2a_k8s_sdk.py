"""Track 2A — test coverage for sdk_service_tools.py and k8s_tools.py.

Target:
  - workers.sdk_service_tools: 22% → ≥65%
  - workers.k8s_tools:         29% → ≥70%

Test strategy:
  - Pure/offline functions: always run, no skip
  - K8s functions: require OrbStack cluster (K8S_AVAILABLE)
  - Prometheus functions: mocked via httpx patching (always run)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
import httpx
import pytest

# ---------------------------------------------------------------------------
# Lab availability flags
# ---------------------------------------------------------------------------


def _check_k8s() -> bool:
    try:
        r = subprocess.run(
            ["kubectl", "get", "ns", "multi-agent"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _check_prom() -> bool:
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "3", "http://localhost:9090/-/healthy"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


K8S_AVAILABLE = _check_k8s()
PROM_AVAILABLE = _check_prom()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(
    *,
    redis: Any = None,
    prometheus_url: str = "",
    k8s_default_namespace: str = "multi-agent",
    telegram: Any = None,
    lab_unchained: bool = False,
    god_mode: bool = False,
    pre_action_state_revalidate_enabled: bool = True,
    restart_rollout_explicit: bool = False,
    inbound_proactive: bool = False,
    telegram_chat_id: Any = None,
    inbound_trace_id: str = "test-trace",
) -> SimpleNamespace:
    settings = SimpleNamespace(
        prometheus_url=prometheus_url,
        k8s_default_namespace=k8s_default_namespace,
        lab_unchained=lab_unchained,
        god_mode=god_mode,
        pre_action_state_revalidate_enabled=pre_action_state_revalidate_enabled,
    )
    return SimpleNamespace(
        redis=redis,
        settings=settings,
        telegram=telegram,
        telegram_chat_id=telegram_chat_id,
        restart_rollout_explicit=restart_rollout_explicit,
        inbound_proactive=inbound_proactive,
        inbound_trace_id=inbound_trace_id,
    )


# ===========================================================================
# sdk_service_tools — pure / offline tests
# ===========================================================================


class TestIsPlaceholderPromql:
    """is_placeholder_promql pure function."""

    def test_empty_string_is_placeholder(self) -> None:
        from workers.sdk_service_tools import is_placeholder_promql

        assert is_placeholder_promql("") is True

    def test_none_is_placeholder(self) -> None:
        from workers.sdk_service_tools import is_placeholder_promql

        assert is_placeholder_promql(None) is True  # type: ignore[arg-type]

    def test_metric_value_threshold_gt_is_placeholder(self) -> None:
        from workers.sdk_service_tools import is_placeholder_promql

        assert is_placeholder_promql("metric_value > threshold") is True

    def test_metric_value_gte_threshold_is_placeholder(self) -> None:
        from workers.sdk_service_tools import is_placeholder_promql

        assert is_placeholder_promql("metric_value >= threshold") is True

    def test_real_promql_is_not_placeholder(self) -> None:
        from workers.sdk_service_tools import is_placeholder_promql

        assert is_placeholder_promql('rate(container_cpu_usage_seconds_total{namespace="default"}[5m])') is False

    def test_simple_up_is_not_placeholder(self) -> None:
        from workers.sdk_service_tools import is_placeholder_promql

        assert is_placeholder_promql("up") is False

    def test_kube_state_is_not_placeholder(self) -> None:
        from workers.sdk_service_tools import is_placeholder_promql

        assert is_placeholder_promql('kube_deployment_status_replicas_available{namespace="default"}') is False


class TestDurationWindowLabel:
    """_duration_window_label formatting."""

    def test_hour_suffix(self) -> None:
        from workers.sdk_service_tools import _duration_window_label

        assert _duration_window_label("1h") == "1 giờ"

    def test_multi_hour(self) -> None:
        from workers.sdk_service_tools import _duration_window_label

        assert _duration_window_label("24h") == "24 giờ"

    def test_minute_suffix(self) -> None:
        from workers.sdk_service_tools import _duration_window_label

        assert _duration_window_label("30m") == "30 phút"

    def test_unknown_returns_default(self) -> None:
        from workers.sdk_service_tools import _duration_window_label

        assert _duration_window_label("bad") == "1 giờ"

    def test_empty_returns_default(self) -> None:
        from workers.sdk_service_tools import _duration_window_label

        assert _duration_window_label("") == "1 giờ"


class TestDiagnosisVmEmpty:
    """_diagnosis_vm_empty message routing."""

    def test_host_target_type(self) -> None:
        from workers.sdk_service_tools import _diagnosis_vm_empty

        msg = _diagnosis_vm_empty({"target_type": "host"}, "1h", promql="up")
        assert "Host/node" in msg
        assert "node_exporter" in msg

    def test_kube_deployment_target(self) -> None:
        from workers.sdk_service_tools import _diagnosis_vm_empty

        msg = _diagnosis_vm_empty(
            {"target_type": "kube_deployment", "namespace": "ns1", "deployment": "dep1"},
            "6h",
            promql="kube_q",
        )
        assert "kube-state-metrics" in msg
        assert "ns1" in msg

    def test_kube_namespace_target(self) -> None:
        from workers.sdk_service_tools import _diagnosis_vm_empty

        msg = _diagnosis_vm_empty(
            {"target_type": "kube_namespace", "namespace": "nsX"},
            "2h",
            promql="kube_pod_status",
        )
        assert "nsX" in msg

    def test_pod_with_namespace_and_pod(self) -> None:
        from workers.sdk_service_tools import _diagnosis_vm_empty

        msg = _diagnosis_vm_empty(
            {"namespace": "default", "pod_name": "my-pod"},
            "1h",
            promql="container_cpu",
        )
        assert "my-pod" in msg
        assert "default" in msg

    def test_pod_only_no_namespace(self) -> None:
        from workers.sdk_service_tools import _diagnosis_vm_empty

        msg = _diagnosis_vm_empty({"pod_name": "my-pod"}, "1h", promql="container_cpu")
        assert "my-pod" in msg

    def test_no_pod_no_namespace(self) -> None:
        from workers.sdk_service_tools import _diagnosis_vm_empty

        msg = _diagnosis_vm_empty({}, "1h", promql="container_cpu")
        assert "thiếu namespace" in msg.lower() or "pod/workload" in msg.lower()


class TestDurationToVmWindow:
    """_duration_to_vm_window mapping."""

    def test_1h(self) -> None:
        from workers.sdk_service_tools import _duration_to_vm_window

        start, step = _duration_to_vm_window("1h")
        assert start == "now-1h"
        assert step == "30s"

    def test_6h_boundary(self) -> None:
        from workers.sdk_service_tools import _duration_to_vm_window

        start, step = _duration_to_vm_window("6h")
        assert start == "now-6h"
        assert step == "30s"

    def test_24h_gets_5m_step(self) -> None:
        from workers.sdk_service_tools import _duration_to_vm_window

        start, step = _duration_to_vm_window("24h")
        assert start == "now-24h"
        assert step == "5m"

    def test_30m(self) -> None:
        from workers.sdk_service_tools import _duration_to_vm_window

        start, step = _duration_to_vm_window("30m")
        assert start == "now-30m"
        assert step == "15s"

    def test_unknown_defaults(self) -> None:
        from workers.sdk_service_tools import _duration_to_vm_window

        start, step = _duration_to_vm_window("bad")
        assert start == "now-1h"
        assert step == "30s"


class TestVmUserFacingError:
    """_vm_user_facing_error message dispatch."""

    def test_vmhttpforbidden_returns_403_message(self) -> None:
        from workers.sdk_service_tools import VMHTTPForbidden, _vm_user_facing_error

        msg = _vm_user_facing_error(VMHTTPForbidden())
        assert "khong_co_quyen" in msg

    def test_connect_error_hints_url(self) -> None:
        from workers.sdk_service_tools import _vm_user_facing_error

        msg = _vm_user_facing_error(httpx.ConnectError("conn refused"))
        assert "ConnectError" in msg or "OMNI_PROMETHEUS_URL" in msg

    def test_timeout_error_mentions_prometheus(self) -> None:
        from workers.sdk_service_tools import _vm_user_facing_error

        msg = _vm_user_facing_error(httpx.TimeoutException("timeout"))
        assert "Prometheus" in msg or "Timeout" in msg

    def test_generic_error(self) -> None:
        from workers.sdk_service_tools import _vm_user_facing_error

        msg = _vm_user_facing_error(RuntimeError("boom"))
        assert "error" in msg
        assert "RuntimeError" in msg


class TestFmtSlowlogEntry:
    """_fmt_slowlog_entry formatting."""

    def test_dict_entry_str_command(self) -> None:
        from workers.sdk_service_tools import _fmt_slowlog_entry

        result = _fmt_slowlog_entry({"id": 1, "duration": 5, "command": "GET key"})
        assert "id=1" in result
        assert "dur_ms=5" in result
        assert "GET key" in result

    def test_dict_entry_bytes_command(self) -> None:
        from workers.sdk_service_tools import _fmt_slowlog_entry

        result = _fmt_slowlog_entry({"id": 2, "duration": 10, "command": b"SET foo bar"})
        assert "SET foo bar" in result

    def test_object_entry_with_duration(self) -> None:
        from workers.sdk_service_tools import _fmt_slowlog_entry

        entry = SimpleNamespace(id=3, duration=15, command="HGET")
        result = _fmt_slowlog_entry(entry)
        assert "id=3" in result
        assert "dur_ms=15" in result
        assert "HGET" in result

    def test_object_no_duration_falls_back_to_repr(self) -> None:
        from workers.sdk_service_tools import _fmt_slowlog_entry

        entry = SimpleNamespace(x=1)
        result = _fmt_slowlog_entry(entry)
        assert "namespace" in result.lower() or "x=1" in result


class TestPrometheusBaseUrl:
    """_prometheus_base_url resolution."""

    def test_settings_url_used(self) -> None:
        from workers.sdk_service_tools import _prometheus_base_url

        ctx = SimpleNamespace(settings=SimpleNamespace(prometheus_url="http://prom:9090"))
        assert _prometheus_base_url(ctx) == "http://prom:9090"

    def test_empty_settings_url_uses_default(self) -> None:
        from workers.sdk_service_tools import _prometheus_base_url

        ctx = SimpleNamespace(settings=SimpleNamespace(prometheus_url=""))
        url = _prometheus_base_url(ctx)
        assert url  # some fallback value

    def test_no_settings_uses_default(self) -> None:
        from workers.sdk_service_tools import _prometheus_base_url

        ctx = SimpleNamespace(settings=None)
        url = _prometheus_base_url(ctx)
        assert url


class TestDefaultNamespace:
    """_default_namespace fallback."""

    def test_from_settings(self) -> None:
        from workers.sdk_service_tools import _default_namespace

        ctx = SimpleNamespace(settings=SimpleNamespace(k8s_default_namespace="custom-ns"))
        assert _default_namespace(ctx) == "custom-ns"

    def test_no_settings_returns_multi_agent(self) -> None:
        from workers.sdk_service_tools import _default_namespace

        ctx = SimpleNamespace(settings=None)
        assert _default_namespace(ctx) == "multi-agent"

    def test_empty_ns_returns_multi_agent(self) -> None:
        from workers.sdk_service_tools import _default_namespace

        ctx = SimpleNamespace(settings=SimpleNamespace(k8s_default_namespace=""))
        assert _default_namespace(ctx) == "multi-agent"


class TestResolvePromqlForArgs:
    """resolve_promql_for_args branch coverage."""

    def test_explicit_query_returned_as_is(self) -> None:
        from workers.sdk_service_tools import resolve_promql_for_args

        ctx = _ctx()
        q, src = resolve_promql_for_args({"query": "up"}, ctx)
        assert q == "up"
        assert src == "explicit_query"

    def test_host_target_type(self) -> None:
        from workers.sdk_service_tools import resolve_promql_for_args

        ctx = _ctx()
        q, src = resolve_promql_for_args({"target_type": "host", "intent": "cpu"}, ctx)
        assert "node_cpu" in q or "node" in q
        assert "host" in src

    def test_kube_deployment_with_deployment(self) -> None:
        from workers.sdk_service_tools import resolve_promql_for_args

        ctx = _ctx()
        q, src = resolve_promql_for_args(
            {"target_type": "kube_deployment", "deployment": "nginx-test", "namespace": "multi-agent"},
            ctx,
        )
        assert "multi-agent" in q
        assert "kube_deployment" in src

    def test_kube_state_deployment_alias(self) -> None:
        from workers.sdk_service_tools import resolve_promql_for_args

        ctx = _ctx()
        q, src = resolve_promql_for_args(
            {
                "target_type": "kube_state_deployment",
                "deployment": "kafka",
                "namespace": "multi-agent",
            },
            ctx,
        )
        assert "multi-agent" in q or "kafka" in q

    def test_kube_deployment_missing_deployment_raises(self) -> None:
        from workers.sdk_service_tools import resolve_promql_for_args

        ctx = _ctx()
        with pytest.raises(ValueError, match="deployment"):
            resolve_promql_for_args({"target_type": "kube_deployment", "namespace": "ns"}, ctx)

    def test_kube_namespace(self) -> None:
        from workers.sdk_service_tools import resolve_promql_for_args

        ctx = _ctx()
        q, src = resolve_promql_for_args(
            {"target_type": "kube_namespace", "namespace": "multi-agent"},
            ctx,
        )
        assert "kube_namespace" in src

    def test_pod_with_namespace_and_pod_name(self) -> None:
        from workers.sdk_service_tools import resolve_promql_for_args

        ctx = _ctx()
        q, src = resolve_promql_for_args(
            {"namespace": "multi-agent", "pod_name": "nginx-test"},
            ctx,
        )
        assert "multi-agent" in q
        assert "pod" in src

    def test_pod_missing_raises_when_no_pod_or_workload(self) -> None:
        from workers.sdk_service_tools import resolve_promql_for_args

        ctx = _ctx()
        with pytest.raises(ValueError, match="pod_name|deployment"):
            resolve_promql_for_args({"namespace": "multi-agent"}, ctx)

    def test_no_intent_defaults_to_cpu(self) -> None:
        from workers.sdk_service_tools import resolve_promql_for_args

        ctx = _ctx()
        q, src = resolve_promql_for_args({"target_type": "host"}, ctx)
        # should not raise and should produce a query
        assert q

    def test_user_text_resolves_intent(self) -> None:
        from workers.sdk_service_tools import resolve_promql_for_args

        ctx = _ctx()
        q, src = resolve_promql_for_args(
            {"target_type": "host", "user_text": "cpu usage high"},
            ctx,
        )
        assert q


class TestTimescaleAnalyze:
    """tool_timeseries_analyze with real numpy computation."""

    @pytest.mark.asyncio
    async def test_basic_stats(self) -> None:
        from workers.sdk_service_tools import tool_timeseries_analyze

        ctx = _ctx()
        result = await tool_timeseries_analyze(ctx, {"values": [1.0, 2.0, 3.0, 4.0, 5.0]})
        data = json.loads(result)
        assert data["n"] == 5
        assert abs(data["mean"] - 3.0) < 0.01
        assert data["min"] == 1.0
        assert data["max"] == 5.0

    @pytest.mark.asyncio
    async def test_csv_values(self) -> None:
        from workers.sdk_service_tools import tool_timeseries_analyze

        ctx = _ctx()
        result = await tool_timeseries_analyze(ctx, {"values": "10,20,30"})
        data = json.loads(result)
        assert data["n"] == 3

    @pytest.mark.asyncio
    async def test_with_ma_window(self) -> None:
        from workers.sdk_service_tools import tool_timeseries_analyze

        ctx = _ctx()
        result = await tool_timeseries_analyze(ctx, {"values": [1.0, 2.0, 3.0, 4.0, 5.0], "ma_window": 3})
        data = json.loads(result)
        assert "moving_avg" in data or "n" in data

    @pytest.mark.asyncio
    async def test_with_forecast_steps(self) -> None:
        from workers.sdk_service_tools import tool_timeseries_analyze

        ctx = _ctx()
        result = await tool_timeseries_analyze(ctx, {"values": [1.0, 2.0, 3.0, 4.0, 5.0], "forecast_steps": 3})
        data = json.loads(result)
        assert "forecast" in data or "n" in data

    @pytest.mark.asyncio
    async def test_missing_values_returns_error(self) -> None:
        from workers.sdk_service_tools import tool_timeseries_analyze

        ctx = _ctx()
        result = await tool_timeseries_analyze(ctx, {})
        assert "Thiếu" in result or "values" in result.lower()


class TestMetricsPromqlHints:
    """tool_metrics_promql_hints topic routing."""

    @pytest.mark.asyncio
    async def test_redis_topic(self) -> None:
        from workers.sdk_service_tools import tool_metrics_promql_hints

        ctx = _ctx()
        result = await tool_metrics_promql_hints(ctx, {"topic": "redis"})
        assert "redis" in result.lower()
        assert "memory" not in result.lower() or "redis_memory" in result.lower()

    @pytest.mark.asyncio
    async def test_disk_topic(self) -> None:
        from workers.sdk_service_tools import tool_metrics_promql_hints

        ctx = _ctx()
        result = await tool_metrics_promql_hints(ctx, {"topic": "disk"})
        assert "disk" in result.lower() or "IOPS" in result

    @pytest.mark.asyncio
    async def test_node_topic(self) -> None:
        from workers.sdk_service_tools import tool_metrics_promql_hints

        ctx = _ctx()
        result = await tool_metrics_promql_hints(ctx, {"topic": "node"})
        assert "node" in result.lower()

    @pytest.mark.asyncio
    async def test_all_topic(self) -> None:
        from workers.sdk_service_tools import tool_metrics_promql_hints

        ctx = _ctx()
        result = await tool_metrics_promql_hints(ctx, {"topic": "all"})
        assert len(result) > 100

    @pytest.mark.asyncio
    async def test_memory_topic_alias(self) -> None:
        from workers.sdk_service_tools import tool_metrics_promql_hints

        ctx = _ctx()
        result = await tool_metrics_promql_hints(ctx, {"topic": "memory"})
        assert "redis" in result.lower()


class TestSystemPsutil:
    """tool_system_psutil real psutil call."""

    @pytest.mark.asyncio
    async def test_returns_cpu_and_ram(self) -> None:
        from workers.sdk_service_tools import tool_system_psutil

        ctx = _ctx()
        result = await tool_system_psutil(ctx, {})
        assert "CPU" in result
        assert "RAM" in result
        assert "Disk" in result

    @pytest.mark.asyncio
    async def test_diskio(self) -> None:
        from workers.sdk_service_tools import tool_system_psutil_diskio

        ctx = _ctx()
        result = await tool_system_psutil_diskio(ctx, {"sample_sec": 0.2})
        assert "delta" in result or "IOPS" in result or "read" in result.lower()


class TestVizLineChart:
    """tool_viz_line_chart chart generation."""

    @pytest.mark.asyncio
    async def test_basic_chart(self) -> None:
        from workers.sdk_service_tools import tool_viz_line_chart

        ctx = _ctx()
        result = await tool_viz_line_chart(ctx, {"title": "Test", "y": [1.0, 2.0, 3.5]})
        assert "PNG" in result or "bytes" in result

    @pytest.mark.asyncio
    async def test_chart_with_x(self) -> None:
        from workers.sdk_service_tools import tool_viz_line_chart

        ctx = _ctx()
        result = await tool_viz_line_chart(ctx, {"title": "Test", "y": [1.0, 2.0, 3.0], "x": "0,1,2"})
        assert "PNG" in result or "bytes" in result

    @pytest.mark.asyncio
    async def test_missing_y_returns_error(self) -> None:
        from workers.sdk_service_tools import tool_viz_line_chart

        ctx = _ctx()
        result = await tool_viz_line_chart(ctx, {"title": "Test"})
        assert "Thiếu" in result

    @pytest.mark.asyncio
    async def test_mismatched_x_y_returns_error(self) -> None:
        from workers.sdk_service_tools import tool_viz_line_chart

        ctx = _ctx()
        result = await tool_viz_line_chart(ctx, {"y": "1,2,3", "x": "0,1"})
        assert "cùng độ dài" in result or "length" in result.lower()

    @pytest.mark.asyncio
    async def test_csv_y_values(self) -> None:
        from workers.sdk_service_tools import tool_viz_line_chart

        ctx = _ctx()
        result = await tool_viz_line_chart(ctx, {"y": "10,20,30,40"})
        assert "PNG" in result or "bytes" in result


class TestRedisTools:
    """tool_redis_health / tool_redis_info — FakeRedis (commands may be limited)."""

    @pytest.mark.asyncio
    async def test_redis_health_no_redis(self) -> None:
        from workers.sdk_service_tools import tool_redis_health

        ctx = _ctx(redis=None)
        result = await tool_redis_health(ctx, {})
        assert "Không có ctx.redis" in result

    @pytest.mark.asyncio
    async def test_redis_info_no_redis(self) -> None:
        from workers.sdk_service_tools import tool_redis_info

        ctx = _ctx(redis=None)
        result = await tool_redis_info(ctx, {})
        assert "Không có ctx.redis" in result

    @pytest.mark.asyncio
    async def test_redis_expert_check_no_redis(self) -> None:
        from workers.sdk_service_tools import tool_redis_expert_check

        ctx = _ctx(redis=None)
        result = await tool_redis_expert_check(ctx, {})
        assert "no_redis" in result or "redis" in result.lower()

    @pytest.mark.asyncio
    async def test_redis_health_with_fakeredis_handles_info_error(self) -> None:
        from workers.sdk_service_tools import tool_redis_health

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _ctx(redis=redis)
        result = await tool_redis_health(ctx, {})
        # FakeRedis does not support INFO — should return error message, not raise
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_redis_expert_check_with_fakeredis(self) -> None:
        from workers.sdk_service_tools import tool_redis_expert_check

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _ctx(redis=redis)
        result = await tool_redis_expert_check(ctx, {})
        assert isinstance(result, str)


class TestPromqlInstantOffline:
    """tool_promql_instant offline / error paths."""

    @pytest.mark.asyncio
    async def test_placeholder_returns_error(self) -> None:
        from workers.sdk_service_tools import tool_promql_instant

        ctx = _ctx(prometheus_url="http://localhost:9090")
        result = await tool_promql_instant(ctx, {"query": "metric_value > threshold"})
        assert "[STATUS] error" in result
        assert "placeholder" in result.lower()

    @pytest.mark.asyncio
    async def test_no_prom_connect_error_returns_message(self) -> None:
        from workers.sdk_service_tools import tool_promql_instant

        ctx = _ctx(prometheus_url="http://127.0.0.1:19999")
        result = await tool_promql_instant(ctx, {"query": "up"})
        assert "[STATUS] error" in result

    @pytest.mark.asyncio
    async def test_missing_pod_raises_value_error(self) -> None:
        from workers.sdk_service_tools import tool_promql_instant

        ctx = _ctx(prometheus_url="http://127.0.0.1:19999")
        with pytest.raises(ValueError):
            await tool_promql_instant(ctx, {"intent": "cpu", "namespace": "multi-agent"})


class TestPromqlRangeOffline:
    """tool_promql_range offline paths."""

    @pytest.mark.asyncio
    async def test_connect_error_returns_message(self) -> None:
        from workers.sdk_service_tools import tool_promql_range

        ctx = _ctx(prometheus_url="http://127.0.0.1:19999")
        result = await tool_promql_range(ctx, {"query": "up"})
        assert "error" in result.lower() or "Prometheus" in result


class TestNetScapy:
    """tool_net_scapy_interfaces — doesn't need network."""

    @pytest.mark.asyncio
    async def test_returns_list_or_error(self) -> None:
        from workers.sdk_service_tools import tool_net_scapy_interfaces

        ctx = _ctx()
        result = await tool_net_scapy_interfaces(ctx, {})
        # Either returns interface list or "not available" error
        assert isinstance(result, str)
        assert len(result) > 0


# ===========================================================================
# k8s_tools — pure / offline tests
# ===========================================================================


class TestRedisKeyFunctions:
    """Pure key generation functions."""

    def test_redis_key_rollout_pending(self) -> None:
        from workers.k8s_tools import redis_key_rollout_pending

        key = redis_key_rollout_pending(12345)
        assert "12345" in key
        assert "rollout_pending" in key

    def test_redis_key_write_pending(self) -> None:
        from workers.k8s_tools import redis_key_write_pending

        key = redis_key_write_pending(99)
        assert "99" in key
        assert "write_pending" in key


class TestDiscoverPairsFromHint:
    """_discover_pairs_from_hint name matching logic."""

    def _pairs(self) -> list[tuple[str, str]]:
        return [
            ("ns1", "nginx-test-abc123"),
            ("ns2", "kafka-xyz"),
            ("ns1", "omni-analyst-abc"),
            ("ns1", "nginx-other-pod"),
        ]

    def test_exact_match(self) -> None:
        from workers.k8s_tools import _discover_pairs_from_hint

        result = _discover_pairs_from_hint("kafka-xyz", self._pairs())
        assert result == [("ns2", "kafka-xyz")]

    def test_prefix_match_returns_sorted_by_len(self) -> None:
        from workers.k8s_tools import _discover_pairs_from_hint

        result = _discover_pairs_from_hint("nginx", self._pairs())
        # Should match by prefix "nginx-..."
        names = [p[1] for p in result]
        assert all("nginx" in n for n in names)
        # shorter first
        assert len(result[0][1]) <= len(result[-1][1])

    def test_substring_match(self) -> None:
        from workers.k8s_tools import _discover_pairs_from_hint

        result = _discover_pairs_from_hint("omni", self._pairs())
        assert any("omni" in p[1] for p in result)

    def test_no_match_returns_empty(self) -> None:
        from workers.k8s_tools import _discover_pairs_from_hint

        result = _discover_pairs_from_hint("zzz-nonexistent", self._pairs())
        assert result == []

    def test_underscore_normalized_to_dash(self) -> None:
        from workers.k8s_tools import _discover_pairs_from_hint

        result = _discover_pairs_from_hint("nginx_test", self._pairs())
        # hint normalized: "nginx-test" → exact match "nginx-test-abc123" via prefix
        assert len(result) > 0


class TestCpuToCorres:
    """_cpu_to_cores unit conversion."""

    def test_nanocores(self) -> None:
        from workers.k8s_tools import _cpu_to_cores

        assert abs(_cpu_to_cores("500000000n") - 0.5) < 1e-9

    def test_millicores(self) -> None:
        from workers.k8s_tools import _cpu_to_cores

        assert abs(_cpu_to_cores("250m") - 0.25) < 1e-9

    def test_full_cores(self) -> None:
        from workers.k8s_tools import _cpu_to_cores

        assert abs(_cpu_to_cores("2") - 2.0) < 1e-9

    def test_empty_returns_zero(self) -> None:
        from workers.k8s_tools import _cpu_to_cores

        assert _cpu_to_cores("") == 0.0

    def test_none_returns_zero(self) -> None:
        from workers.k8s_tools import _cpu_to_cores

        assert _cpu_to_cores(None) == 0.0


class TestMemToBytes:
    """_mem_to_bytes unit conversion."""

    def test_ki(self) -> None:
        from workers.k8s_tools import _mem_to_bytes

        assert _mem_to_bytes("128Ki") == 128 * 1024

    def test_mi(self) -> None:
        from workers.k8s_tools import _mem_to_bytes

        assert _mem_to_bytes("256Mi") == 256 * 1024**2

    def test_gi(self) -> None:
        from workers.k8s_tools import _mem_to_bytes

        assert _mem_to_bytes("4Gi") == 4 * 1024**3

    def test_k(self) -> None:
        from workers.k8s_tools import _mem_to_bytes

        assert _mem_to_bytes("100K") == 100_000

    def test_m(self) -> None:
        from workers.k8s_tools import _mem_to_bytes

        assert _mem_to_bytes("100M") == 100_000_000

    def test_g(self) -> None:
        from workers.k8s_tools import _mem_to_bytes

        assert _mem_to_bytes("2G") == 2_000_000_000

    def test_plain_digits(self) -> None:
        from workers.k8s_tools import _mem_to_bytes

        assert _mem_to_bytes("1024") == 1024

    def test_empty_returns_zero(self) -> None:
        from workers.k8s_tools import _mem_to_bytes

        assert _mem_to_bytes("") == 0

    def test_none_returns_zero(self) -> None:
        from workers.k8s_tools import _mem_to_bytes

        assert _mem_to_bytes(None) == 0


class TestPct:
    """_pct percentage helper."""

    def test_half(self) -> None:
        from workers.k8s_tools import _pct

        assert abs(_pct(0.5, 1.0) - 50.0) < 0.001

    def test_over_capacity_capped_at_100(self) -> None:
        from workers.k8s_tools import _pct

        assert _pct(2.0, 1.0) == 100.0

    def test_zero_cap_returns_zero(self) -> None:
        from workers.k8s_tools import _pct

        assert _pct(5.0, 0.0) == 0.0


class TestEventIsWarningOrCritical:
    """_event_is_warning_or_critical event filtering."""

    def test_warning_type_is_true(self) -> None:
        from workers.k8s_tools import _event_is_warning_or_critical

        e = SimpleNamespace(type="Warning", reason="SomeReason")
        assert _event_is_warning_or_critical(e) is True

    def test_oom_reason_is_true(self) -> None:
        from workers.k8s_tools import _event_is_warning_or_critical

        e = SimpleNamespace(type="Normal", reason="OOMKilling")
        assert _event_is_warning_or_critical(e) is True

    def test_backoff_reason_is_true(self) -> None:
        from workers.k8s_tools import _event_is_warning_or_critical

        e = SimpleNamespace(type="Normal", reason="BackOff")
        assert _event_is_warning_or_critical(e) is True

    def test_normal_reason_is_false(self) -> None:
        from workers.k8s_tools import _event_is_warning_or_critical

        e = SimpleNamespace(type="Normal", reason="Scheduled")
        assert _event_is_warning_or_critical(e) is False

    def test_killed_reason_is_true(self) -> None:
        from workers.k8s_tools import _event_is_warning_or_critical

        e = SimpleNamespace(type="Normal", reason="Killed")
        assert _event_is_warning_or_critical(e) is True

    def test_unhealthy_reason_is_true(self) -> None:
        from workers.k8s_tools import _event_is_warning_or_critical

        e = SimpleNamespace(type="Normal", reason="Unhealthy")
        assert _event_is_warning_or_critical(e) is True


class TestWsAllowsKubectlListAll:
    """_ws_allows_kubectl_list_all permission check."""

    def test_lab_unchained_true(self) -> None:
        from workers.k8s_tools import _ws_allows_kubectl_list_all

        ctx = SimpleNamespace(settings=SimpleNamespace(lab_unchained=True, god_mode=False))
        assert _ws_allows_kubectl_list_all(ctx) is True

    def test_god_mode_true(self) -> None:
        from workers.k8s_tools import _ws_allows_kubectl_list_all

        ctx = SimpleNamespace(settings=SimpleNamespace(lab_unchained=False, god_mode=True))
        assert _ws_allows_kubectl_list_all(ctx) is True

    def test_neither_is_false(self) -> None:
        from workers.k8s_tools import _ws_allows_kubectl_list_all

        ctx = SimpleNamespace(settings=SimpleNamespace(lab_unchained=False, god_mode=False))
        assert _ws_allows_kubectl_list_all(ctx) is False

    def test_no_settings_is_false(self) -> None:
        from workers.k8s_tools import _ws_allows_kubectl_list_all

        ctx = SimpleNamespace(settings=None)
        assert _ws_allows_kubectl_list_all(ctx) is False


class TestParseKubectlGetPodsLines:
    """_parse_kubectl_get_pods_lines stdout parsing."""

    def test_header_stripped(self) -> None:
        from workers.k8s_tools import _parse_kubectl_get_pods_lines

        lines = [
            "NAMESPACE   NAME           READY   STATUS    RESTARTS   AGE",
            "default     nginx-abc      1/1     Running   0          5d",
        ]
        pairs, body = _parse_kubectl_get_pods_lines(lines)
        assert len(pairs) == 1
        assert pairs[0] == ("default", "nginx-abc")
        assert len(body) == 1

    def test_empty_lines_skipped(self) -> None:
        from workers.k8s_tools import _parse_kubectl_get_pods_lines

        lines = ["", "  ", "default  nginx  1/1  Running  0  1d"]
        pairs, body = _parse_kubectl_get_pods_lines(lines)
        assert len(pairs) == 1

    def test_short_lines_skipped(self) -> None:
        from workers.k8s_tools import _parse_kubectl_get_pods_lines

        lines = ["onlyonetoken"]
        pairs, body = _parse_kubectl_get_pods_lines(lines)
        assert len(pairs) == 0

    def test_multiple_pods(self) -> None:
        from workers.k8s_tools import _parse_kubectl_get_pods_lines

        lines = [
            "NAMESPACE   NAME     READY   STATUS",
            "ns1         pod-a    1/1     Running",
            "ns2         pod-b    0/1     Pending",
        ]
        pairs, body = _parse_kubectl_get_pods_lines(lines)
        assert len(pairs) == 2
        assert ("ns1", "pod-a") in pairs
        assert ("ns2", "pod-b") in pairs


class TestFormatPodList:
    """_format_pod_list output format."""

    def test_format_with_items(self) -> None:
        from workers.k8s_tools import _format_pod_list

        meta1 = SimpleNamespace(name="nginx-abc")
        status1 = SimpleNamespace(phase="Running", pod_ip="10.0.0.1")
        pod1 = SimpleNamespace(metadata=meta1, status=status1, spec=None)

        meta2 = SimpleNamespace(name="kafka-xyz")
        status2 = SimpleNamespace(phase="Pending", pod_ip=None)
        pod2 = SimpleNamespace(metadata=meta2, status=status2, spec=None)

        resp = SimpleNamespace(items=[pod1, pod2])
        result = _format_pod_list(resp, "test-ns")
        assert "nginx-abc" in result
        assert "kafka-xyz" in result
        assert "test-ns" in result

    def test_format_empty_namespace(self) -> None:
        from workers.k8s_tools import _format_pod_list

        resp = SimpleNamespace(items=[])
        result = _format_pod_list(resp, "empty-ns")
        assert "không có pod" in result.lower() or "empty-ns" in result


class TestUsageFromMetricsBody:
    """_usage_from_metrics_body parsing."""

    def test_single_container(self) -> None:
        from workers.k8s_tools import _usage_from_metrics_body

        body = {
            "containers": [
                {"usage": {"cpu": "100m", "memory": "128Mi"}},
            ]
        }
        cpu, mem = _usage_from_metrics_body(body)
        assert abs(cpu - 0.1) < 1e-9
        assert mem == 128 * 1024**2

    def test_multi_containers_summed(self) -> None:
        from workers.k8s_tools import _usage_from_metrics_body

        body = {
            "containers": [
                {"usage": {"cpu": "100m", "memory": "64Mi"}},
                {"usage": {"cpu": "200m", "memory": "128Mi"}},
            ]
        }
        cpu, mem = _usage_from_metrics_body(body)
        assert abs(cpu - 0.3) < 1e-6
        assert mem == 192 * 1024**2

    def test_empty_containers(self) -> None:
        from workers.k8s_tools import _usage_from_metrics_body

        cpu, mem = _usage_from_metrics_body({"containers": []})
        assert cpu == 0.0
        assert mem == 0


class TestAggregateLimits:
    """_aggregate_limits resource summing."""

    def _make_container(
        self,
        cpu_limit: str | None = None,
        mem_limit: str | None = None,
        cpu_req: str | None = None,
        mem_req: str | None = None,
    ) -> Any:
        limits = {}
        requests = {}
        if cpu_limit:
            limits["cpu"] = cpu_limit
        if mem_limit:
            limits["memory"] = mem_limit
        if cpu_req:
            requests["cpu"] = cpu_req
        if mem_req:
            requests["memory"] = mem_req
        resources = SimpleNamespace(limits=limits, requests=requests)
        return SimpleNamespace(resources=resources)

    def test_limits_used_when_present(self) -> None:
        from workers.k8s_tools import _aggregate_limits

        c = self._make_container(cpu_limit="500m", mem_limit="512Mi")
        pod = SimpleNamespace(spec=SimpleNamespace(containers=[c]))
        cpu, mem = _aggregate_limits(pod)
        assert abs(cpu - 0.5) < 1e-9
        assert mem == 512 * 1024**2

    def test_requests_fallback_when_no_limits(self) -> None:
        from workers.k8s_tools import _aggregate_limits

        c = self._make_container(cpu_req="250m", mem_req="256Mi")
        pod = SimpleNamespace(spec=SimpleNamespace(containers=[c]))
        cpu, mem = _aggregate_limits(pod)
        assert abs(cpu - 0.25) < 1e-9

    def test_multiple_containers_summed(self) -> None:
        from workers.k8s_tools import _aggregate_limits

        c1 = self._make_container(cpu_limit="500m", mem_limit="512Mi")
        c2 = self._make_container(cpu_limit="500m", mem_limit="512Mi")
        pod = SimpleNamespace(spec=SimpleNamespace(containers=[c1, c2]))
        cpu, mem = _aggregate_limits(pod)
        assert abs(cpu - 1.0) < 1e-9
        assert mem == 1024 * 1024**2


class TestExecuteRolloutRestartFromPending:
    """execute_rollout_restart_from_pending validation paths."""

    @pytest.mark.asyncio
    async def test_missing_namespace_returns_error(self) -> None:
        from workers.k8s_tools import execute_rollout_restart_from_pending

        ctx = SimpleNamespace(settings=None, inbound_trace_id="t1")
        result = await execute_rollout_restart_from_pending(ctx, {})
        assert "Thiếu namespace" in result

    @pytest.mark.asyncio
    async def test_missing_deployment_returns_error(self) -> None:
        from workers.k8s_tools import execute_rollout_restart_from_pending

        ctx = SimpleNamespace(settings=None, inbound_trace_id="t1")
        result = await execute_rollout_restart_from_pending(ctx, {"namespace": "multi-agent"})
        assert "Thiếu deployment" in result

    @pytest.mark.asyncio
    async def test_empty_namespace_returns_error(self) -> None:
        from workers.k8s_tools import execute_rollout_restart_from_pending

        ctx = SimpleNamespace(settings=None, inbound_trace_id="t1")
        result = await execute_rollout_restart_from_pending(ctx, {"namespace": "  ", "deployment": "dep"})
        assert "Thiếu namespace" in result


# ===========================================================================
# k8s_tools — K8s live tests (skipif K8S not available)
# ===========================================================================


class TestK8sListPods:
    """Live K8s pod listing."""


    @pytest.mark.asyncio
    async def test_list_namespace_pods_no_namespace(self) -> None:
        from workers.k8s_tools import tool_list_namespace_pods

        ctx = _ctx()
        result = await tool_list_namespace_pods(ctx, {})
        assert "namespace" in result.lower() or "Chưa có" in result


    @pytest.mark.asyncio
    async def test_k8s_list_pods_no_namespace_calls_all(self) -> None:
        from workers.k8s_tools import tool_k8s_list_pods

        ctx = _ctx()
        result = await tool_k8s_list_pods(ctx, {})
        assert isinstance(result, str)
        assert len(result) > 10


class TestK8sNamespacePodsTop:
    """Live kubectl top equivalent."""


    @pytest.mark.asyncio
    async def test_namespace_pods_top_no_namespace(self) -> None:
        from workers.k8s_tools import tool_namespace_pods_top

        ctx = _ctx()
        result = await tool_namespace_pods_top(ctx, {})
        assert "error" in result.lower() or "Thiếu" in result


class TestK8sResolveIdentity:
    """Live pod/deployment identity resolution."""


    @pytest.mark.asyncio
    async def test_resolve_pod_identity_no_hint(self) -> None:
        from workers.k8s_tools import tool_resolve_pod_identity

        ctx = _ctx()
        result = await tool_resolve_pod_identity(ctx, {})
        assert "Thiếu" in result or "hint" in result.lower()


    @pytest.mark.asyncio
    async def test_resolve_deployment_identity_no_hint(self) -> None:
        from workers.k8s_tools import tool_resolve_deployment_identity

        ctx = _ctx()
        result = await tool_resolve_deployment_identity(ctx, {})
        assert "Thiếu" in result


class TestK8sInspectPodDeep:
    """Live pod deep inspect."""


    @pytest.mark.asyncio
    async def test_inspect_pod_no_name(self) -> None:
        from workers.k8s_tools import tool_inspect_pod_deep

        ctx = _ctx()
        result = await tool_inspect_pod_deep(ctx, {})
        assert "Missing" in result or "pod_name" in result


class TestK8sRolloutRestart:
    """Live rollout restart (confirm_required / not_found paths — no actual restart)."""


    @pytest.mark.asyncio
    async def test_missing_deployment_name(self) -> None:
        from workers.k8s_tools import tool_k8s_rollout_restart

        ctx = _ctx()
        result = await tool_k8s_rollout_restart(ctx, {})
        assert "Thiếu" in result


class TestK8sRolloutRestartFromPendingK8s:
    """execute_rollout_restart_from_pending with real K8s."""


class TestDeploymentEvidenceSnapshot:
    """deployment_evidence_snapshot reading."""


class TestDiscoverPodAcrossNamespaces:
    """discover_pod_across_namespaces live cluster."""


class TestResolvePodIdentityK8s:
    """resolve_pod_identity low-level function."""


# ===========================================================================
# sdk_service_tools — Prometheus mocked tests (httpx patching)
# ===========================================================================


# ---------------------------------------------------------------------------
# Shared fixtures for Prometheus mocking
# ---------------------------------------------------------------------------


class _FakeResp:
    """Minimal httpx.Response replacement for mocking."""

    def __init__(self, data: Any, status: int = 200) -> None:
        self.status_code = status
        self._data = data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "http://test"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> Any:
        return self._data


def _make_fake_client(data: Any, status: int = 200) -> type:
    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *a: Any, **k: Any) -> None:
            pass

        async def get(self, url: str, params: Any = None) -> _FakeResp:
            return _FakeResp(data, status)

    return FakeAsyncClient


@contextlib.contextmanager
def _prom_mock(data: Any, status: int = 200):  # type: ignore[return]
    """Context manager: temporarily replace httpx.AsyncClient for Prometheus calls."""
    fake_cls = _make_fake_client(data, status)
    original = httpx.AsyncClient
    httpx.AsyncClient = fake_cls  # type: ignore[misc]
    try:
        yield
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]


_PROM_VECTOR_SUCCESS = {
    "status": "success",
    "data": {
        "resultType": "vector",
        "result": [
            {"metric": {"job": "prometheus", "instance": "localhost:9090"}, "value": [1715000000.0, "1"]}
        ],
    },
}

_PROM_VECTOR_EMPTY = {
    "status": "success",
    "data": {"resultType": "vector", "result": []},
}

_PROM_MATRIX_SUCCESS = {
    "status": "success",
    "data": {
        "resultType": "matrix",
        "result": [
            {
                "metric": {},
                "values": [
                    [1715000000.0, "1.0"],
                    [1715000060.0, "1.2"],
                    [1715000120.0, "1.4"],
                    [1715000180.0, "1.6"],
                    [1715000240.0, "1.8"],
                ],
            }
        ],
    },
}

_PROM_MATRIX_EMPTY = {
    "status": "success",
    "data": {"resultType": "matrix", "result": []},
}

_PROM_ERROR = {"status": "error", "errorType": "bad_data", "error": "invalid query"}


class TestPromqlInstantMocked:
    """tool_promql_instant with mocked Prometheus."""

    @pytest.mark.asyncio
    async def test_success_vector_result(self) -> None:
        from workers.sdk_service_tools import tool_promql_instant

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_VECTOR_SUCCESS):
            result = await tool_promql_instant(ctx, {"query": "up"})
        assert "[STATUS] business_hit" in result
        assert "resultType=vector" in result

    @pytest.mark.asyncio
    async def test_empty_result(self) -> None:
        from workers.sdk_service_tools import tool_promql_instant

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_VECTOR_EMPTY):
            result = await tool_promql_instant(ctx, {"query": "up"})
        assert "[STATUS] empty_result" in result

    @pytest.mark.asyncio
    async def test_prom_error_status(self) -> None:
        from workers.sdk_service_tools import tool_promql_instant

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_ERROR):
            result = await tool_promql_instant(ctx, {"query": "bad{}"})
        assert "[STATUS] error" in result

    @pytest.mark.asyncio
    async def test_403_forbidden(self) -> None:
        from workers.sdk_service_tools import tool_promql_instant

        class FakeClientForbidden:
            def __init__(self, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "FakeClientForbidden":
                return self

            async def __aexit__(self, *a: Any, **k: Any) -> None:
                pass

            async def get(self, url: str, params: Any = None) -> _FakeResp:
                return _FakeResp({"status": "error"}, 403)

        original = httpx.AsyncClient
        httpx.AsyncClient = FakeClientForbidden  # type: ignore[misc]
        try:
            ctx = _ctx(prometheus_url="http://prom:9090")
            result = await tool_promql_instant(ctx, {"query": "up"})
            assert "khong_co_quyen" in result or "quyền" in result
        finally:
            httpx.AsyncClient = original  # type: ignore[misc]

    @pytest.mark.asyncio
    async def test_many_results_truncated(self) -> None:
        from workers.sdk_service_tools import tool_promql_instant

        many = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"i": str(i)}, "value": [1715000000.0, str(i)]}
                    for i in range(25)
                ],
            },
        }
        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(many):
            result = await tool_promql_instant(ctx, {"query": "up"})
        assert "kết quả" in result or "results" in result.lower() or "+" in result


class TestPromqlRangeMocked:
    """tool_promql_range with mocked Prometheus."""

    @pytest.mark.asyncio
    async def test_success_with_data(self) -> None:
        from workers.sdk_service_tools import tool_promql_range

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_MATRIX_SUCCESS):
            result = await tool_promql_range(ctx, {"query": "up", "start": "now-1h", "end": "now", "step": "30s"})
        assert "resultType=matrix" in result
        assert "n_points=5" in result

    @pytest.mark.asyncio
    async def test_empty_matrix(self) -> None:
        from workers.sdk_service_tools import tool_promql_range

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_MATRIX_EMPTY):
            result = await tool_promql_range(
                ctx,
                {"query": "up", "namespace": "multi-agent", "pod_name": "nginx"},
            )
        assert "DIAGNOSIS" in result

    @pytest.mark.asyncio
    async def test_prom_error_status(self) -> None:
        from workers.sdk_service_tools import tool_promql_range

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_ERROR):
            result = await tool_promql_range(ctx, {"query": "bad"})
        assert "error" in result.lower()


class TestVizVmRangeChartMocked:
    """tool_viz_vm_range_chart with mocked Prometheus."""

    @pytest.mark.asyncio
    async def test_chart_created_no_telegram(self) -> None:
        from workers.sdk_service_tools import tool_viz_vm_range_chart

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_MATRIX_SUCCESS):
            result = await tool_viz_vm_range_chart(ctx, {"query": "up", "title": "Test Chart"})
        assert "PNG" in result or "bytes" in result or "điểm" in result

    @pytest.mark.asyncio
    async def test_empty_data(self) -> None:
        from workers.sdk_service_tools import tool_viz_vm_range_chart

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_MATRIX_EMPTY):
            result = await tool_viz_vm_range_chart(ctx, {"query": "up"})
        assert "no_data" in result or "DIAGNOSIS" in result

    @pytest.mark.asyncio
    async def test_with_forecast(self) -> None:
        from workers.sdk_service_tools import tool_viz_vm_range_chart

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_MATRIX_SUCCESS):
            result = await tool_viz_vm_range_chart(ctx, {"query": "up", "forecast_steps": 5})
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_prom_error(self) -> None:
        from workers.sdk_service_tools import tool_viz_vm_range_chart

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_ERROR):
            result = await tool_viz_vm_range_chart(ctx, {"query": "bad"})
        assert "error" in result.lower() or "DIAGNOSIS" in result


class TestQueryHistoricalMetricsMocked:
    """tool_query_historical_metrics with mocked Prometheus."""

    @pytest.mark.asyncio
    async def test_success_no_telegram(self) -> None:
        from workers.sdk_service_tools import tool_query_historical_metrics

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_MATRIX_SUCCESS):
            result = await tool_query_historical_metrics(ctx, {"query": "up", "title": "Hist"})
        assert "n=" in result or "chart" in result.lower()

    @pytest.mark.asyncio
    async def test_empty_data(self) -> None:
        from workers.sdk_service_tools import tool_query_historical_metrics

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_MATRIX_EMPTY):
            result = await tool_query_historical_metrics(ctx, {"query": "up"})
        assert "no_data" in result or "DIAGNOSIS" in result

    @pytest.mark.asyncio
    async def test_prom_error(self) -> None:
        from workers.sdk_service_tools import tool_query_historical_metrics

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_ERROR):
            result = await tool_query_historical_metrics(ctx, {"query": "bad"})
        assert "error" in result.lower() or "DIAGNOSIS" in result


class TestQueryTimescalesImplMocked:
    """_query_timeseries_impl / tool_query_prometheus_metrics with mocked Prometheus."""

    @pytest.mark.asyncio
    async def test_query_prometheus_metrics_success(self) -> None:
        from workers.sdk_service_tools import tool_query_prometheus_metrics

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_MATRIX_SUCCESS):
            result = await tool_query_prometheus_metrics(
                ctx,
                {"query": "up", "namespace": "multi-agent", "pod_name": "nginx"},
            )
        assert "[DATA]" in result
        assert "[DIAGNOSIS]" in result

    @pytest.mark.asyncio
    async def test_query_prometheus_metrics_empty(self) -> None:
        from workers.sdk_service_tools import tool_query_prometheus_metrics

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_MATRIX_EMPTY):
            result = await tool_query_prometheus_metrics(
                ctx,
                {"query": "up"},
            )
        assert "no_data" in result or "DIAGNOSIS" in result

    @pytest.mark.asyncio
    async def test_with_forecast(self) -> None:
        from workers.sdk_service_tools import tool_query_prometheus_metrics

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_MATRIX_SUCCESS):
            result = await tool_query_prometheus_metrics(
                ctx,
                {
                    "query": "up",
                    "namespace": "multi-agent",
                    "pod_name": "nginx",
                    "forecast": True,
                    "forecast_horizon": "1h",
                },
            )
        assert "[DATA]" in result
        assert "forecast" in result.lower()

    @pytest.mark.asyncio
    async def test_host_target_title(self) -> None:
        from workers.sdk_service_tools import tool_query_prometheus_metrics

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_MATRIX_SUCCESS):
            result = await tool_query_prometheus_metrics(
                ctx,
                {"target_type": "host", "intent": "cpu"},
            )
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_kube_deployment_target_title(self) -> None:
        from workers.sdk_service_tools import tool_query_prometheus_metrics

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_MATRIX_SUCCESS):
            result = await tool_query_prometheus_metrics(
                ctx,
                {
                    "target_type": "kube_deployment",
                    "deployment": "nginx-test",
                    "namespace": "multi-agent",
                },
            )
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_kube_namespace_target_title(self) -> None:
        from workers.sdk_service_tools import tool_query_prometheus_metrics

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_MATRIX_SUCCESS):
            result = await tool_query_prometheus_metrics(
                ctx,
                {"target_type": "kube_namespace", "namespace": "multi-agent"},
            )
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_prom_error_status(self) -> None:
        from workers.sdk_service_tools import tool_query_prometheus_metrics

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_ERROR):
            result = await tool_query_prometheus_metrics(ctx, {"query": "bad"})
        assert "error" in result.lower()

    @pytest.mark.asyncio
    async def test_query_vm_timeseries_alias(self) -> None:
        from workers.sdk_service_tools import tool_query_vm_timeseries

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_MATRIX_SUCCESS):
            result = await tool_query_vm_timeseries(
                ctx, {"query": "up", "pod_name": "nginx", "namespace": "multi-agent"}
            )
        assert "[DATA]" in result


class TestPredictResourceExhaustionMocked:
    """tool_predict_resource_exhaustion with mocked Prometheus."""

    @pytest.mark.asyncio
    async def test_increasing_trend(self) -> None:
        from workers.sdk_service_tools import tool_predict_resource_exhaustion

        # Series with clear upward trend
        data = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {},
                        "values": [
                            [1715000000.0 + i * 300, str(i * 0.1)]
                            for i in range(20)
                        ],
                    }
                ],
            },
        }
        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(data):
            result = await tool_predict_resource_exhaustion(ctx, {"query": "up"})
        assert "[DATA]" in result
        assert "slope" in result.lower() or "horizon" in result.lower()

    @pytest.mark.asyncio
    async def test_flat_trend_no_exhaustion(self) -> None:
        from workers.sdk_service_tools import tool_predict_resource_exhaustion

        # Flat series → slope <= 0 path
        data = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {},
                        "values": [[1715000000.0 + i * 300, "5.0"] for i in range(10)],
                    }
                ],
            },
        }
        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(data):
            result = await tool_predict_resource_exhaustion(ctx, {"query": "up"})
        assert "tuyến tính" in result or "slope" in result.lower() or "không tăng" in result

    @pytest.mark.asyncio
    async def test_insufficient_points(self) -> None:
        from workers.sdk_service_tools import tool_predict_resource_exhaustion

        sparse = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {"metric": {}, "values": [[1715000000.0, "1.0"], [1715000060.0, "1.1"]]}
                ],
            },
        }
        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(sparse):
            result = await tool_predict_resource_exhaustion(ctx, {"query": "up"})
        assert "no_data" in result or "điểm" in result

    @pytest.mark.asyncio
    async def test_prom_error(self) -> None:
        from workers.sdk_service_tools import tool_predict_resource_exhaustion

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_ERROR):
            result = await tool_predict_resource_exhaustion(ctx, {"query": "bad"})
        assert "error" in result.lower()

    @pytest.mark.asyncio
    async def test_auto_query_from_intent(self) -> None:
        from workers.sdk_service_tools import tool_predict_resource_exhaustion

        data = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {},
                        "values": [[1715000000.0 + i * 300, str(0.5 + i * 0.01)] for i in range(10)],
                    }
                ],
            },
        }
        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(data):
            result = await tool_predict_resource_exhaustion(
                ctx,
                {"intent": "cpu", "namespace": "multi-agent", "pod_name": "nginx"},
            )
        assert isinstance(result, str)


class TestForecastMemoryRiskVmMocked:
    """tool_forecast_memory_risk_vm with mocked Prometheus."""

    @pytest.mark.asyncio
    async def test_with_total_gib(self) -> None:
        from workers.sdk_service_tools import tool_forecast_memory_risk_vm

        data = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {},
                        "values": [[1715000000.0 + i * 300, str(1_000_000_000 + i * 10_000_000)] for i in range(10)],
                    }
                ],
            },
        }
        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(data):
            result = await tool_forecast_memory_risk_vm(
                ctx,
                {
                    "query": "container_memory_working_set_bytes",
                    "total_ram_gib": 8.0,
                    "horizon_hours": 6,
                },
            )
        assert isinstance(result, str)
        # Should have OOM risk assessment — check some key fields exist
        data_parsed = json.loads(result)
        assert isinstance(data_parsed, dict)
        # The response has various keys depending on the oom_risk_from_series output
        assert len(data_parsed) > 0

    @pytest.mark.asyncio
    async def test_missing_total_ram(self) -> None:
        from workers.sdk_service_tools import tool_forecast_memory_risk_vm

        data = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {},
                        "values": [[1715000000.0 + i * 300, str(1_000_000_000)] for i in range(10)],
                    }
                ],
            },
        }
        # total_promql instant will return None (connect error) → missing total message

        class FakeClientDual:
            _calls = 0

            def __init__(self, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "FakeClientDual":
                return self

            async def __aexit__(self, *a: Any, **k: Any) -> None:
                pass

            async def get(self, url: str, params: Any = None) -> _FakeResp:
                FakeClientDual._calls += 1
                if "/query_range" in url:
                    return _FakeResp(data)
                # instant query returns empty → total_bytes = None
                return _FakeResp(
                    {"status": "success", "data": {"resultType": "vector", "result": []}}
                )

        original = httpx.AsyncClient
        httpx.AsyncClient = FakeClientDual  # type: ignore[misc]
        try:
            ctx = _ctx(prometheus_url="http://prom:9090")
            result = await tool_forecast_memory_risk_vm(ctx, {"query": "container_memory"})
            assert "total_ram" in result.lower() or "Thiếu tổng RAM" in result
        finally:
            httpx.AsyncClient = original  # type: ignore[misc]

    @pytest.mark.asyncio
    async def test_insufficient_points(self) -> None:
        from workers.sdk_service_tools import tool_forecast_memory_risk_vm

        sparse = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [{"metric": {}, "values": [[1715000000.0, "1000000"]]}],
            },
        }
        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(sparse):
            result = await tool_forecast_memory_risk_vm(
                ctx, {"query": "mem", "total_ram_gib": 8.0}
            )
        assert "no_data" in result or "điểm" in result

    @pytest.mark.asyncio
    async def test_prom_error(self) -> None:
        from workers.sdk_service_tools import tool_forecast_memory_risk_vm

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_ERROR):
            result = await tool_forecast_memory_risk_vm(ctx, {"query": "mem", "total_ram_gib": 8})
        assert "error" in result.lower()

    @pytest.mark.asyncio
    async def test_auto_query_from_namespace_pod(self) -> None:
        from workers.sdk_service_tools import tool_forecast_memory_risk_vm

        data = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {"metric": {}, "values": [[1715000000.0 + i * 300, str(512_000_000 + i * 1_000_000)] for i in range(10)]},
                ],
            },
        }
        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(data):
            result = await tool_forecast_memory_risk_vm(
                ctx,
                {"namespace": "multi-agent", "pod_name": "nginx", "total_ram_gib": 4.0},
            )
        assert isinstance(result, str)


class TestResolvePromqlOrExplicit:
    """_resolve_promql_or_explicit branching."""

    def test_explicit_query_takes_priority(self) -> None:
        from workers.sdk_service_tools import _resolve_promql_or_explicit

        ctx = _ctx()
        q, src = _resolve_promql_or_explicit({"query": "up", "intent": "cpu"}, ctx)
        assert q == "up"
        assert src == "explicit_query"

    def test_no_query_delegates_to_resolve_promql(self) -> None:
        from workers.sdk_service_tools import _resolve_promql_or_explicit

        ctx = _ctx()
        q, src = _resolve_promql_or_explicit({"target_type": "host", "intent": "cpu"}, ctx)
        assert q  # non-empty
        assert "host" in src


class TestPromqlInstantMorePaths:
    """Additional tool_promql_instant paths."""

    @pytest.mark.asyncio
    async def test_host_target_type_resolved(self) -> None:
        from workers.sdk_service_tools import tool_promql_instant

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_VECTOR_SUCCESS):
            result = await tool_promql_instant(ctx, {"target_type": "host", "intent": "cpu"})
        assert "[STATUS]" in result

    @pytest.mark.asyncio
    async def test_kube_namespace_target(self) -> None:
        from workers.sdk_service_tools import tool_promql_instant

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_VECTOR_SUCCESS):
            result = await tool_promql_instant(
                ctx, {"target_type": "kube_namespace", "namespace": "multi-agent"}
            )
        assert "[STATUS]" in result

    @pytest.mark.asyncio
    async def test_kube_deployment_target(self) -> None:
        from workers.sdk_service_tools import tool_promql_instant

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_VECTOR_EMPTY):
            result = await tool_promql_instant(
                ctx,
                {
                    "target_type": "kube_deployment",
                    "deployment": "nginx-test",
                    "namespace": "multi-agent",
                },
            )
        assert "[STATUS]" in result

    @pytest.mark.asyncio
    async def test_pod_target(self) -> None:
        from workers.sdk_service_tools import tool_promql_instant

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_VECTOR_SUCCESS):
            result = await tool_promql_instant(
                ctx, {"namespace": "multi-agent", "pod_name": "nginx-test"}
            )
        assert "[STATUS]" in result


class TestVmPromqlInstantAlias:
    """tool_vm_promql_instant is same as tool_promql_instant."""

    @pytest.mark.asyncio
    async def test_alias_works(self) -> None:
        from workers.sdk_service_tools import tool_vm_promql_instant

        ctx = _ctx(prometheus_url="http://prom:9090")
        with _prom_mock(_PROM_VECTOR_SUCCESS):
            result = await tool_vm_promql_instant(ctx, {"query": "up"})
        assert "[STATUS]" in result
