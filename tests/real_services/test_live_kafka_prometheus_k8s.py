"""Live service coverage hooks: real clients only, no mocks and no in-process doubles.

These tests require live infrastructure. If the required env vars or services are
not reachable, tests FAIL explicitly (no silent skip — zero-skip policy).
"""

from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_live_kafka_producer_start_stop() -> None:
    bootstrap = (os.environ.get("OMNI_KAFKA_BOOTSTRAP_SERVERS") or "").strip()
    if not bootstrap:
        pytest.fail(
            "OMNI_KAFKA_BOOTSTRAP_SERVERS unset — set it to a reachable Kafka broker "
            "before running live integration tests"
        )

    from messaging.kafka_bus import create_producer

    producer = await create_producer(bootstrap)
    await producer.stop()


@pytest.mark.asyncio
async def test_live_prometheus_query_up() -> None:
    base = (os.environ.get("OMNI_PROMETHEUS_URL") or "").strip()
    if not base:
        pytest.fail(
            "OMNI_PROMETHEUS_URL unset — set it to a reachable Prometheus endpoint "
            "before running live integration tests"
        )

    from workers.sdk_service_tools import _prometheus_get_json
    from workers.settings import WorkerSettings

    settings = WorkerSettings(prometheus_url=base)
    data = await _prometheus_get_json(
        settings,
        "/api/v1/query",
        {"query": "up"},
    )
    assert data.get("status") == "success"


@pytest.mark.asyncio
async def test_live_prometheus_range_dataframe() -> None:
    base = (os.environ.get("OMNI_PROMETHEUS_URL") or "").strip()
    if not base:
        pytest.fail(
            "OMNI_PROMETHEUS_URL unset — set it to a reachable Prometheus endpoint "
            "before running live integration tests"
        )

    from metrics.prometheus_dataframe import fetch_range_dataframe
    from workers.settings import WorkerSettings

    end = int(time.time())
    start = end - 120
    settings = WorkerSettings(prometheus_url=base)
    df = await fetch_range_dataframe(
        settings,
        promql="up",
        start=str(start),
        end=str(end),
        step="60s",
    )
    assert list(df.columns) == ["ds", "y"]


@pytest.mark.asyncio
async def test_live_kubernetes_list_namespaces_readonly() -> None:
    if not (os.environ.get("KUBECONFIG") or os.environ.get("KUBERNETES_SERVICE_HOST")):
        pytest.fail(
            "KUBECONFIG or KUBERNETES_SERVICE_HOST unset — "
            "K8s cluster access required for this live test"
        )

    from kubernetes_asyncio import client
    from workers.k8s_tools import _load_k8s_config

    await _load_k8s_config()
    api = client.CoreV1Api()
    try:
        namespaces = await api.list_namespace(limit=1)
    finally:
        await api.api_client.close()
    assert namespaces is not None
