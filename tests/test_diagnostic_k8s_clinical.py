"""PodMetrics probe: 404 từ metrics.k8s.io → INCONCLUSIVE (không FAILED)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client import ApiException

from workers.diagnostic_k8s_clinical import (
    probe_k8s_clinical_pod_log_previous,
    probe_k8s_clinical_pod_log_tail,
    probe_k8s_clinical_pod_metrics,
)
from workers.proactive_models import AnomalyEvent


@pytest.mark.asyncio
async def test_pod_metrics_404_is_inconclusive_not_failed() -> None:
    cq = json.dumps(
        {
            "labels": {"namespace": "multi-agent", "pod": "nginx-test-abc"},
            "annotations": {},
        }
    )
    ev = AnomalyEvent(
        trace_id="t-404",
        canonical_query=cq,
        error_hint="HighCPU 90%",
        namespace="multi-agent",
    )
    ctx = MagicMock()

    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object = AsyncMock(
        side_effect=ApiException(status=404, reason="Not Found")
    )
    fake_api.api_client = MagicMock()
    fake_api.api_client.close = AsyncMock()

    with (
        patch("workers.diagnostic_k8s_clinical._load_k8s_config", new_callable=AsyncMock),
        patch("workers.diagnostic_k8s_clinical.client.CustomObjectsApi", return_value=fake_api),
    ):
        raw = await probe_k8s_clinical_pod_metrics(ctx, ev)

    assert raw.probe_name == "k8s_clinical_pod_metrics"
    assert raw.status == "INCONCLUSIVE"
    assert "404" in raw.raw_text
    assert raw.structured_hint and raw.structured_hint.get("omit_reason") == "podmetrics_not_found_404"


@pytest.mark.asyncio
async def test_log_tail_skipped_for_pending_create_container_error_uses_events() -> None:
    cq = json.dumps(
        {
            "labels": {"namespace": "ns", "pod": "bad-pod"},
            "annotations": {},
        }
    )
    ev = AnomalyEvent(
        trace_id="t-log",
        canonical_query=cq,
        error_hint="x",
        namespace="ns",
    )
    ctx = MagicMock()

    fake_pod = MagicMock()
    fake_pod.status.phase = "Pending"
    cs = MagicMock()
    cs.name = "nginx"
    cs.state = MagicMock()
    cs.state.waiting = MagicMock()
    cs.state.waiting.reason = "CreateContainerError"
    cs.state.terminated = None
    fake_pod.status.container_statuses = [cs]
    fake_pod.spec.containers = [MagicMock(name="nginx")]

    fake_ev = MagicMock()
    fake_ev.items = []

    fake_v1 = MagicMock()
    fake_v1.read_namespaced_pod = AsyncMock(return_value=fake_pod)
    fake_v1.read_namespaced_pod_log = AsyncMock(return_value="should-not-be-called")
    fake_v1.list_namespaced_event = AsyncMock(return_value=fake_ev)
    fake_v1.api_client = MagicMock()
    fake_v1.api_client.close = AsyncMock()

    with (
        patch("workers.diagnostic_k8s_clinical._load_k8s_config", new_callable=AsyncMock),
        patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=fake_v1),
    ):
        raw = await probe_k8s_clinical_pod_log_tail(ctx, ev)

    assert raw.probe_name == "k8s_clinical_pod_log_tail"
    assert raw.status == "PASSED"
    assert "skipped pod log" in raw.raw_text.lower()
    assert raw.structured_hint.get("kind") == "PodEvents"
    assert fake_v1.read_namespaced_pod_log.call_count == 0


@pytest.mark.asyncio
async def test_log_tail_skipped_for_pending_create_container_config_error() -> None:
    cq = json.dumps({"labels": {"namespace": "ns", "pod": "bad-pod"}, "annotations": {}})
    ev = AnomalyEvent(trace_id="t-cfg", canonical_query=cq, error_hint="x", namespace="ns")
    ctx = MagicMock()
    fake_pod = MagicMock()
    fake_pod.status.phase = "Pending"
    cs = MagicMock()
    cs.name = "nginx"
    cs.state = MagicMock()
    cs.state.waiting = MagicMock()
    cs.state.waiting.reason = "CreateContainerConfigError"
    cs.state.terminated = None
    fake_pod.status.container_statuses = [cs]
    fake_pod.spec.containers = [MagicMock(name="nginx")]
    fake_ev = MagicMock()
    fake_ev.items = []
    fake_v1 = MagicMock()
    fake_v1.read_namespaced_pod = AsyncMock(return_value=fake_pod)
    fake_v1.read_namespaced_pod_log = AsyncMock(return_value="no")
    fake_v1.list_namespaced_event = AsyncMock(return_value=fake_ev)
    fake_v1.api_client = MagicMock()
    fake_v1.api_client.close = AsyncMock()
    with (
        patch("workers.diagnostic_k8s_clinical._load_k8s_config", new_callable=AsyncMock),
        patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=fake_v1),
    ):
        raw = await probe_k8s_clinical_pod_log_tail(ctx, ev)
    assert fake_v1.read_namespaced_pod_log.call_count == 0
    assert "skipped pod log" in raw.raw_text.lower()


@pytest.mark.asyncio
async def test_log_previous_uses_kube_previous_true() -> None:
    cq = json.dumps({"labels": {"namespace": "ns", "pod": "crashy"}, "annotations": {}})
    ev = AnomalyEvent(
        trace_id="t-prev",
        canonical_query=cq,
        error_hint="x",
        namespace="ns",
    )
    ctx = MagicMock()

    fake_pod = MagicMock()
    fake_pod.status.phase = "Running"
    cs = MagicMock()
    cs.name = "nginx"
    cs.state = MagicMock()
    cs.state.waiting = MagicMock()
    cs.state.waiting.reason = "CrashLoopBackOff"
    cs.state.terminated = None
    cs.restart_count = 3
    fake_pod.status.container_statuses = [cs]
    fake_pod.spec.containers = [MagicMock(name="nginx")]

    fake_v1 = MagicMock()
    fake_v1.read_namespaced_pod = AsyncMock(return_value=fake_pod)
    fake_v1.read_namespaced_pod_log = AsyncMock(return_value="prev-instance-line")
    fake_v1.api_client = MagicMock()
    fake_v1.api_client.close = AsyncMock()

    with (
        patch("workers.diagnostic_k8s_clinical._load_k8s_config", new_callable=AsyncMock),
        patch("workers.diagnostic_k8s_clinical.client.CoreV1Api", return_value=fake_v1),
    ):
        raw = await probe_k8s_clinical_pod_log_previous(ctx, ev)

    assert raw.probe_name == "k8s_clinical_pod_log_previous"
    assert raw.status == "PASSED"
    assert raw.structured_hint.get("k8s_log_previous") is True
    fake_v1.read_namespaced_pod_log.assert_awaited_once()
    call_kw = fake_v1.read_namespaced_pod_log.await_args
    assert call_kw is not None
    assert call_kw.kwargs.get("previous") is True
