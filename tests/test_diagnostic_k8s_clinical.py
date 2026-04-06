"""PodMetrics probe: 404 từ metrics.k8s.io → INCONCLUSIVE (không FAILED)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client import ApiException

from workers.diagnostic_k8s_clinical import probe_k8s_clinical_pod_metrics
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
