"""Coverage tests for workers.diagnostic_probe_registry — targets uncovered async paths."""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

os.environ.setdefault("OMNI_ENV_MODE", "dev")
os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379/0")

from workers import diagnostic_probe_registry as dpr
from workers.proactive_models import AnomalyEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ns(fields: dict[str, Any]) -> SimpleNamespace:
    ns = SimpleNamespace()
    for k, v in fields.items():
        if isinstance(v, dict):
            setattr(ns, k, _ns(v))
        elif isinstance(v, list):
            setattr(ns, k, [_ns(i) if isinstance(i, dict) else i for i in v])
        else:
            setattr(ns, k, v)
    return ns


def _make_ev(namespace: str = "multi-agent", pod: str = "svc-abc1234-xyzab", deployment: str = "svc") -> AnomalyEvent:
    return AnomalyEvent(
        trace_id="trace-reg-001",
        canonical_query="{}",
        namespace=namespace,
        deployment=deployment,
        gigo_metadata={"pod": pod},
    )


def _make_ctx(prometheus_url: str = "http://prometheus:9090") -> MagicMock:
    ctx = MagicMock()
    ctx.settings = SimpleNamespace(
        k8s_default_namespace="multi-agent",
        prometheus_url=prometheus_url,
        kafka_topic_alerts="omni-alerts",
        kafka_bootstrap_servers="localhost:9092",
    )
    ctx.redis = AsyncMock()
    ctx.kafka = AsyncMock()
    return ctx


def _fake_api_client() -> MagicMock:
    ac = MagicMock()
    ac.close = AsyncMock()
    return ac


def _make_prom_success(value: str = "0.5") -> dict:
    return {
        "status": "success",
        "data": {
            "result": [{"metric": {"pod": "p1", "container": "c1"}, "value": [1.0, value]}]
        },
    }


def _make_prom_empty() -> dict:
    return {"status": "success", "data": {"result": []}}


# ---------------------------------------------------------------------------
# probe_redis_ping
# ---------------------------------------------------------------------------

class TestProbeRedisPing:
    async def test_ping_success_true(self):
        ctx = _make_ctx()
        ctx.redis.ping = AsyncMock(return_value=True)
        ev = _make_ev()
        result = await dpr.probe_redis_ping(ctx, ev)
        assert result.status == "PASSED"

    async def test_ping_success_pong_bytes(self):
        ctx = _make_ctx()
        ctx.redis.ping = AsyncMock(return_value=b"PONG")
        ev = _make_ev()
        result = await dpr.probe_redis_ping(ctx, ev)
        assert result.status == "PASSED"

    async def test_ping_unexpected_value_inconclusive(self):
        ctx = _make_ctx()
        ctx.redis.ping = AsyncMock(return_value=False)
        ev = _make_ev()
        result = await dpr.probe_redis_ping(ctx, ev)
        assert result.status == "INCONCLUSIVE"

    async def test_ping_exception_failed(self):
        ctx = _make_ctx()
        ctx.redis.ping = AsyncMock(side_effect=Exception("connection refused"))
        ev = _make_ev()
        result = await dpr.probe_redis_ping(ctx, ev)
        assert result.status == "FAILED"


# ---------------------------------------------------------------------------
# probe_k8s_list_pods_namespace
# ---------------------------------------------------------------------------

class TestProbeK8sListPodsNamespace:
    async def test_passed_when_pods_found(self):
        ctx = _make_ctx()
        ev = _make_ev(namespace="multi-agent")

        pod_list = SimpleNamespace(items=["pod1", "pod2", "pod3"])
        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.list_namespaced_pod = AsyncMock(return_value=pod_list)

        with (
            patch("workers.diagnostic_probe_registry._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_probe_registry.client.CoreV1Api", return_value=v1_mock),
        ):
            result = await dpr.probe_k8s_list_pods_namespace(ctx, ev)

        assert result.status == "PASSED"
        assert result.structured_hint["pod_count"] == 3

    async def test_failed_on_exception(self):
        ctx = _make_ctx()
        ev = _make_ev()

        with (
            patch("workers.diagnostic_probe_registry._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_probe_registry.client.CoreV1Api", side_effect=RuntimeError("err")),
        ):
            result = await dpr.probe_k8s_list_pods_namespace(ctx, ev)

        assert result.status == "FAILED"

    async def test_uses_default_namespace_when_ev_ns_empty(self):
        ctx = _make_ctx()
        ev = _make_ev(namespace="")

        pod_list = SimpleNamespace(items=[])
        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.list_namespaced_pod = AsyncMock(return_value=pod_list)

        with (
            patch("workers.diagnostic_probe_registry._load_k8s_config", new=AsyncMock()),
            patch("workers.diagnostic_probe_registry.client.CoreV1Api", return_value=v1_mock),
        ):
            result = await dpr.probe_k8s_list_pods_namespace(ctx, ev)

        assert result.status == "PASSED"
        call_args = v1_mock.list_namespaced_pod.call_args
        assert call_args[1]["namespace"] == "multi-agent"


# ---------------------------------------------------------------------------
# probe_prom_pod_cpu_cores
# ---------------------------------------------------------------------------

class TestProbePodCpuCores:
    async def test_skipped_no_namespace(self):
        ctx = _make_ctx()
        ev = AnomalyEvent(trace_id="trace-skip", canonical_query="{}", namespace="", deployment="")
        result = await dpr.probe_prom_pod_cpu_cores(ctx, ev)
        assert result.status == "SKIPPED"

    async def test_passed_with_data(self):
        ctx = _make_ctx()
        ev = _make_ev()

        with patch.object(dpr, "_prometheus_instant_query", new=AsyncMock(return_value=_make_prom_success())):
            result = await dpr.probe_prom_pod_cpu_cores(ctx, ev)

        assert result.status == "PASSED"

    async def test_inconclusive_empty_result(self):
        ctx = _make_ctx()
        ev = _make_ev()

        with patch.object(dpr, "_prometheus_instant_query", new=AsyncMock(return_value=_make_prom_empty())):
            result = await dpr.probe_prom_pod_cpu_cores(ctx, ev)

        assert result.status == "INCONCLUSIVE"

    async def test_failed_on_exception(self):
        ctx = _make_ctx()
        ev = _make_ev()

        with patch.object(dpr, "_prometheus_instant_query", new=AsyncMock(side_effect=Exception("timeout"))):
            result = await dpr.probe_prom_pod_cpu_cores(ctx, ev)

        assert result.status == "FAILED"


# ---------------------------------------------------------------------------
# probe_prom_pod_memory_wss
# ---------------------------------------------------------------------------

class TestProbePodMemoryWss:
    async def test_skipped_no_workload(self):
        ctx = _make_ctx()
        ev = AnomalyEvent(trace_id="trace-skip", canonical_query="{}", namespace="", deployment="")
        result = await dpr.probe_prom_pod_memory_wss(ctx, ev)
        assert result.status == "SKIPPED"

    async def test_passed_with_data(self):
        ctx = _make_ctx()
        ev = _make_ev()

        with patch.object(dpr, "_prometheus_instant_query", new=AsyncMock(return_value=_make_prom_success("134217728"))):
            result = await dpr.probe_prom_pod_memory_wss(ctx, ev)

        assert result.status == "PASSED"

    async def test_inconclusive_empty_result(self):
        ctx = _make_ctx()
        ev = _make_ev()

        with patch.object(dpr, "_prometheus_instant_query", new=AsyncMock(return_value=_make_prom_empty())):
            result = await dpr.probe_prom_pod_memory_wss(ctx, ev)

        assert result.status == "INCONCLUSIVE"

    async def test_failed_on_exception(self):
        ctx = _make_ctx()
        ev = _make_ev()

        with patch.object(dpr, "_prometheus_instant_query", new=AsyncMock(side_effect=Exception("boom"))):
            result = await dpr.probe_prom_pod_memory_wss(ctx, ev)

        assert result.status == "FAILED"


# ---------------------------------------------------------------------------
# probe_kafka_alerts_topic
# ---------------------------------------------------------------------------

class TestProbeKafkaAlertsTopic:
    async def test_always_passed(self):
        ctx = _make_ctx()
        ev = _make_ev()
        result = await dpr.probe_kafka_alerts_topic(ctx, ev)
        assert result.status == "PASSED"
        assert "kafka" in result.raw_text.lower() or "topic" in result.raw_text.lower()


# ---------------------------------------------------------------------------
# probe_node_disk_pressure
# ---------------------------------------------------------------------------

class TestProbeNodeDiskPressure:
    async def test_failed_when_result_present(self):
        ctx = _make_ctx()
        ev = _make_ev()

        with patch.object(dpr, "_prometheus_instant_query", new=AsyncMock(return_value=_make_prom_success("0.05"))):
            result = await dpr.probe_node_disk_pressure(ctx, ev)

        assert result.status == "FAILED"
        assert "10%" in result.raw_text

    async def test_passed_when_empty_result(self):
        ctx = _make_ctx()
        ev = _make_ev()

        with patch.object(dpr, "_prometheus_instant_query", new=AsyncMock(return_value=_make_prom_empty())):
            result = await dpr.probe_node_disk_pressure(ctx, ev)

        assert result.status == "PASSED"

    async def test_inconclusive_on_exception(self):
        ctx = _make_ctx()
        ev = _make_ev()

        with patch.object(dpr, "_prometheus_instant_query", new=AsyncMock(side_effect=Exception("err"))):
            result = await dpr.probe_node_disk_pressure(ctx, ev)

        assert result.status == "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# probe_node_cpu_saturation
# ---------------------------------------------------------------------------

class TestProbeNodeCpuSaturation:
    async def test_passed_with_data(self):
        ctx = _make_ctx()
        ev = _make_ev()

        with patch.object(dpr, "_prometheus_instant_query", new=AsyncMock(return_value=_make_prom_success("0.75"))):
            result = await dpr.probe_node_cpu_saturation(ctx, ev)

        assert result.status == "PASSED"

    async def test_inconclusive_empty(self):
        ctx = _make_ctx()
        ev = _make_ev()

        with patch.object(dpr, "_prometheus_instant_query", new=AsyncMock(return_value=_make_prom_empty())):
            result = await dpr.probe_node_cpu_saturation(ctx, ev)

        assert result.status == "INCONCLUSIVE"

    async def test_inconclusive_on_exception(self):
        ctx = _make_ctx()
        ev = _make_ev()

        with patch.object(dpr, "_prometheus_instant_query", new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
            result = await dpr.probe_node_cpu_saturation(ctx, ev)

        assert result.status == "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# probe_node_memory_pressure
# ---------------------------------------------------------------------------

class TestProbeNodeMemoryPressure:
    async def test_passed_with_data(self):
        ctx = _make_ctx()
        ev = _make_ev()

        with patch.object(dpr, "_prometheus_instant_query", new=AsyncMock(return_value=_make_prom_success("0.85"))):
            result = await dpr.probe_node_memory_pressure(ctx, ev)

        assert result.status == "PASSED"

    async def test_inconclusive_on_error(self):
        ctx = _make_ctx()
        ev = _make_ev()

        with patch.object(dpr, "_prometheus_instant_query", new=AsyncMock(side_effect=Exception("timeout"))):
            result = await dpr.probe_node_memory_pressure(ctx, ev)

        assert result.status == "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# probe_node_disk_io_saturation
# ---------------------------------------------------------------------------

class TestProbeNodeDiskIoSaturation:
    async def test_passed_with_data(self):
        ctx = _make_ctx()
        ev = _make_ev()

        with patch.object(dpr, "_prometheus_instant_query", new=AsyncMock(return_value=_make_prom_success("0.3"))):
            result = await dpr.probe_node_disk_io_saturation(ctx, ev)

        assert result.status == "PASSED"

    async def test_inconclusive_on_exception(self):
        ctx = _make_ctx()
        ev = _make_ev()

        with patch.object(dpr, "_prometheus_instant_query", new=AsyncMock(side_effect=Exception("err"))):
            result = await dpr.probe_node_disk_io_saturation(ctx, ev)

        assert result.status == "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# probe_k8s_service_endpoints_ready
# ---------------------------------------------------------------------------

class TestProbeServiceEndpointsReady:
    async def test_skipped_no_namespace(self):
        ctx = _make_ctx()
        ev = AnomalyEvent(trace_id="trace-skip2", canonical_query="{}", namespace="", deployment="", gigo_metadata={})
        result = await dpr.probe_k8s_service_endpoints_ready(ctx, ev)
        assert result.status == "SKIPPED"

    async def test_skipped_no_service(self):
        ctx = _make_ctx()
        ev = AnomalyEvent(trace_id="trace-ns01", canonical_query="{}", namespace="ns", deployment="", gigo_metadata={})
        result = await dpr.probe_k8s_service_endpoints_ready(ctx, ev)
        assert result.status == "SKIPPED"

    async def test_passed_with_ready_endpoints(self):
        ctx = _make_ctx()
        ev = _make_ev(namespace="multi-agent", deployment="my-svc")

        subset = _ns({"addresses": [_ns({"ip": "10.0.0.1"}), _ns({"ip": "10.0.0.2"})]})
        ep = _ns({"subsets": [subset]})

        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.read_namespaced_endpoints = AsyncMock(return_value=ep)

        from kubernetes_asyncio import config as k8s_config
        with (
            patch.object(k8s_config, "load_incluster_config", new=AsyncMock()),
            patch("workers.diagnostic_probe_registry.client.CoreV1Api", return_value=v1_mock),
        ):
            result = await dpr.probe_k8s_service_endpoints_ready(ctx, ev)

        assert result.status == "PASSED"
        assert result.structured_hint["ready_addresses"] == 2

    async def test_failed_no_ready_endpoints(self):
        ctx = _make_ctx()
        ev = _make_ev(namespace="multi-agent", deployment="my-svc")

        subset = _ns({"addresses": []})
        ep = _ns({"subsets": [subset]})

        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.read_namespaced_endpoints = AsyncMock(return_value=ep)

        from kubernetes_asyncio import config as k8s_config
        with (
            patch.object(k8s_config, "load_incluster_config", new=AsyncMock()),
            patch("workers.diagnostic_probe_registry.client.CoreV1Api", return_value=v1_mock),
        ):
            result = await dpr.probe_k8s_service_endpoints_ready(ctx, ev)

        assert result.status == "FAILED"
        assert "no ready endpoints" in result.raw_text

    async def test_inconclusive_on_k8s_error(self):
        ctx = _make_ctx()
        ev = _make_ev(namespace="multi-agent", deployment="my-svc")

        v1_mock = AsyncMock()
        v1_mock.api_client = _fake_api_client()
        v1_mock.read_namespaced_endpoints = AsyncMock(side_effect=Exception("not found"))

        from kubernetes_asyncio import config as k8s_config
        with (
            patch.object(k8s_config, "load_incluster_config", new=AsyncMock()),
            patch("workers.diagnostic_probe_registry.client.CoreV1Api", return_value=v1_mock),
        ):
            result = await dpr.probe_k8s_service_endpoints_ready(ctx, ev)

        assert result.status == "INCONCLUSIVE"

    async def test_inconclusive_when_incluster_config_fails(self):
        ctx = _make_ctx()
        ev = _make_ev(namespace="multi-agent", deployment="my-svc")

        from kubernetes_asyncio import config as k8s_config
        with patch.object(k8s_config, "load_incluster_config", new=AsyncMock(side_effect=Exception("not in cluster"))):
            result = await dpr.probe_k8s_service_endpoints_ready(ctx, ev)

        assert result.status == "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# probe_k8s_networkpolicy_audit
# ---------------------------------------------------------------------------

class TestProbeNetworkPolicyAudit:
    async def test_skipped_no_namespace(self):
        ctx = _make_ctx()
        ev = AnomalyEvent(trace_id="trace-loki1", canonical_query="{}", namespace="", gigo_metadata={})
        result = await dpr.probe_k8s_networkpolicy_audit(ctx, ev)
        assert result.status == "SKIPPED"

    async def test_passed_with_policies(self):
        ctx = _make_ctx()
        ev = _make_ev(namespace="multi-agent")

        policy1 = _ns({"metadata": {"name": "deny-all"}})
        policy2 = _ns({"metadata": {"name": "allow-ingress"}})

        netv1_mock = AsyncMock()
        netv1_mock.api_client = _fake_api_client()
        netv1_mock.list_namespaced_network_policy = AsyncMock(
            return_value=SimpleNamespace(items=[policy1, policy2])
        )

        from kubernetes_asyncio import config as k8s_config
        with (
            patch.object(k8s_config, "load_incluster_config", new=AsyncMock()),
            patch("workers.diagnostic_probe_registry.client.NetworkingV1Api", return_value=netv1_mock),
        ):
            result = await dpr.probe_k8s_networkpolicy_audit(ctx, ev)

        assert result.status == "PASSED"
        assert result.structured_hint["policy_count"] == 2
        assert "deny-all" in result.structured_hint["policies"]

    async def test_passed_no_policies(self):
        ctx = _make_ctx()
        ev = _make_ev(namespace="multi-agent")

        netv1_mock = AsyncMock()
        netv1_mock.api_client = _fake_api_client()
        netv1_mock.list_namespaced_network_policy = AsyncMock(
            return_value=SimpleNamespace(items=[])
        )

        from kubernetes_asyncio import config as k8s_config
        with (
            patch.object(k8s_config, "load_incluster_config", new=AsyncMock()),
            patch("workers.diagnostic_probe_registry.client.NetworkingV1Api", return_value=netv1_mock),
        ):
            result = await dpr.probe_k8s_networkpolicy_audit(ctx, ev)

        assert result.status == "PASSED"
        assert result.structured_hint["policy_count"] == 0

    async def test_inconclusive_on_list_error(self):
        ctx = _make_ctx()
        ev = _make_ev(namespace="multi-agent")

        netv1_mock = AsyncMock()
        netv1_mock.api_client = _fake_api_client()
        netv1_mock.list_namespaced_network_policy = AsyncMock(side_effect=Exception("forbidden"))

        from kubernetes_asyncio import config as k8s_config
        with (
            patch.object(k8s_config, "load_incluster_config", new=AsyncMock()),
            patch("workers.diagnostic_probe_registry.client.NetworkingV1Api", return_value=netv1_mock),
        ):
            result = await dpr.probe_k8s_networkpolicy_audit(ctx, ev)

        assert result.status == "INCONCLUSIVE"

    async def test_inconclusive_when_incluster_fails(self):
        ctx = _make_ctx()
        ev = _make_ev(namespace="multi-agent")

        from kubernetes_asyncio import config as k8s_config
        with patch.object(k8s_config, "load_incluster_config", new=AsyncMock(side_effect=Exception("no cluster"))):
            result = await dpr.probe_k8s_networkpolicy_audit(ctx, ev)

        assert result.status == "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# probe_loki_access_log_surge
# ---------------------------------------------------------------------------

class TestProbeLokiAccessLogSurge:
    async def test_skipped_no_namespace(self):
        ctx = _make_ctx()
        ev = AnomalyEvent(trace_id="trace-loki1", canonical_query="{}", namespace="", gigo_metadata={})
        result = await dpr.probe_loki_access_log_surge(ctx, ev)
        assert result.status == "SKIPPED"

    async def test_passed_when_ok(self):
        ctx = _make_ctx()
        ev = _make_ev(namespace="multi-agent")

        surge_result = MagicMock()
        surge_result.ok = True
        surge_result.dominant_error_class = "5xx"
        surge_result.reason = "sustained 5xx surge"
        surge_result.meta = {"count_5xx": 150}

        with patch("workers.log_surge_probe.evaluate_log_surge_sigma_bypass", new=AsyncMock(return_value=surge_result)):
            result = await dpr.probe_loki_access_log_surge(ctx, ev)

        assert result.status == "PASSED"
        assert result.structured_hint["sigma_bypass_eligible"] is True

    async def test_inconclusive_when_not_ok(self):
        ctx = _make_ctx()
        ev = _make_ev(namespace="multi-agent")

        surge_result = MagicMock()
        surge_result.ok = False
        surge_result.dominant_error_class = "none"
        surge_result.reason = "not enough lines"
        surge_result.meta = {}

        with patch("workers.log_surge_probe.evaluate_log_surge_sigma_bypass", new=AsyncMock(return_value=surge_result)):
            result = await dpr.probe_loki_access_log_surge(ctx, ev)

        assert result.status == "INCONCLUSIVE"

    async def test_inconclusive_on_exception(self):
        ctx = _make_ctx()
        ev = _make_ev(namespace="multi-agent")

        with patch("workers.log_surge_probe.evaluate_log_surge_sigma_bypass", new=AsyncMock(side_effect=Exception("loki err"))):
            result = await dpr.probe_loki_access_log_surge(ctx, ev)

        assert result.status == "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# run_probe dispatch
# ---------------------------------------------------------------------------

class TestRunProbe:
    async def test_known_probe_dispatched(self):
        ctx = _make_ctx()
        ev = _make_ev()

        from workers.diagnostic_evidence import ProbeRunRaw

        mock_raw = ProbeRunRaw(
            probe_name="redis_ping",
            status="PASSED",
            raw_text="PONG",
            structured_hint={},
        )
        mock_fn = AsyncMock(return_value=mock_raw)
        with patch.dict(dpr.PROBE_REGISTRY, {"redis_ping": mock_fn}, clear=False):
            result = await dpr.run_probe("redis_ping", ctx, ev)

        assert result.status == "PASSED"

    async def test_unknown_probe_returns_skipped(self):
        ctx = _make_ctx()
        ev = _make_ev()
        result = await dpr.run_probe("totally_unknown_probe_xyz", ctx, ev)
        assert result.status == "SKIPPED"
        assert "unknown" in result.raw_text.lower()

    async def test_all_registry_keys_present(self):
        expected_keys = [
            "redis_ping",
            "k8s_list_pods_namespace",
            "kafka_alerts_topic",
            "k8s_clinical_pod_status",
            "k8s_clinical_pod_metrics",
            "k8s_clinical_pod_log_tail",
            "k8s_clinical_pod_log_previous",
            "k8s_clinical_pod_events",
            "k8s_resource_quota_probe",
            "prom_pod_cpu_cores",
            "prom_pod_memory_wss",
            "node_disk_pressure",
            "node_cpu_saturation",
            "node_memory_pressure",
            "node_disk_io_saturation",
        ]
        for key in expected_keys:
            assert key in dpr.PROBE_REGISTRY, f"probe key missing: {key}"


# ---------------------------------------------------------------------------
# _prometheus_instant_query — URL handling
# ---------------------------------------------------------------------------

class TestPrometheusInstantQuery:
    async def test_missing_url_returns_error(self):
        ctx = _make_ctx(prometheus_url="")
        result = await dpr._prometheus_instant_query(ctx, "up")
        assert result["status"] == "error"
        assert "unset" in result.get("error", "")

    async def test_none_url_returns_error(self):
        ctx = _make_ctx()
        ctx.settings.prometheus_url = None
        result = await dpr._prometheus_instant_query(ctx, "up")
        assert result["status"] == "error"

    async def test_successful_query(self):
        ctx = _make_ctx()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=_make_prom_success())

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("workers.diagnostic_probe_registry.httpx.AsyncClient", return_value=mock_client):
            result = await dpr._prometheus_instant_query(ctx, "up")

        assert result["status"] == "success"

    async def test_http_error_raises(self):
        ctx = _make_ctx()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError("404", request=MagicMock(), response=MagicMock()))

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("workers.diagnostic_probe_registry.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(httpx.HTTPStatusError):
                await dpr._prometheus_instant_query(ctx, "up")
