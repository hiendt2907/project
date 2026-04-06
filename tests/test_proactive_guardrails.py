"""proactive_guardrails: resource ref + Redis key helpers."""

from __future__ import annotations

import pytest

from workers.proactive_guardrails import (
    extract_resource_ref,
    proactive_gigo_cluster_identity_ok,
    proactive_lease_key,
    resource_freeze_redis_key,
)
from workers.proactive_models import AnomalyEvent


def test_extract_resource_ref_patch() -> None:
    r = extract_resource_ref(
        "k8s_patch_resource",
        {"namespace": "prod", "name": "api", "resource_type": "Deployment", "patch_json": "{}"},
    )
    assert r == ("prod", "Deployment", "api")


def test_proactive_gigo_requires_namespace_or_trigger_promql() -> None:
    assert proactive_gigo_cluster_identity_ok(
        AnomalyEvent(trace_id="trace-1", canonical_query="x", namespace="prod")
    ) == (True, "")
    assert proactive_gigo_cluster_identity_ok(
        AnomalyEvent(trace_id="trace-2", canonical_query="x", namespace="", trigger_promql="sum(up)")
    ) == (True, "")
    ok, reason = proactive_gigo_cluster_identity_ok(
        AnomalyEvent(trace_id="trace-3", canonical_query="sum(up)", namespace="", trigger_promql="")
    )
    assert ok is False
    assert "gigo" in reason


def test_extract_resource_ref_rollout() -> None:
    r = extract_resource_ref(
        "k8s_rollout_restart",
        {"namespace": "prod", "deployment": "api"},
    )
    assert r == ("prod", "Deployment", "api")


def test_extract_resource_ref_none_when_missing_ns() -> None:
    assert extract_resource_ref("k8s_rollout_restart", {"deployment": "api"}) is None


def test_resource_freeze_key_stable() -> None:
    k1 = resource_freeze_redis_key("pfx", "ns-a", "Deployment", "web")
    k2 = resource_freeze_redis_key("pfx", "ns-a", "Deployment", "web")
    assert k1 == k2
    assert "ns-a" in k1
    assert "Deployment" in k1


def test_proactive_lease_key_shape() -> None:
    lk = proactive_lease_key("omni:proactive", "k8s_scale_deployment", ("ns", "Deployment", "x"))
    assert lk.startswith("omni:proactive:lease:")


@pytest.mark.asyncio
async def test_try_acquire_lease_nx() -> None:
    from unittest.mock import AsyncMock

    from workers.proactive_guardrails import try_acquire_resource_lease

    r = AsyncMock()
    r.set = AsyncMock(return_value=True)
    ok = await try_acquire_resource_lease(r, "k:1", token="t", ttl_sec=30)
    assert ok is True
    r.set.assert_awaited_once()
