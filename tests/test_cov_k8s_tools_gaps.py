"""
tests/test_cov_k8s_tools_gaps.py

Coverage-gap tests for src/workers/k8s_tools.py
Targets uncovered lines:
  87, 97-98, 120, 124-133, 188-189, 208, 215-216, 230-243,
  303, 311, 317, 366-378, 394-395, 409-416, 457-514, 524, 528-529,
  536-539, 558-559, 561, 587-588, 614-615, 624, 628-631, 650-651,
  669-676, 684-687, 705-706, 712-713, 724, 735-738, 756-757, 762-763,
  770-771, 782-789, 821-825, 845-848, 861, 863, 870-877, 889-894,
  905, 907, 909, 913-920, 928-934, 948-951, 970-971, 984, 989, 991-992,
  1021-1022

All K8s API calls are mocked.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client import ApiException


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ac():
    ac = MagicMock()
    ac.close = AsyncMock()
    return ac


def _v1(**kw):
    v = MagicMock()
    v.api_client = _ac()
    for k, val in kw.items():
        setattr(v, k, val)
    return v


def _apps(**kw):
    a = MagicMock()
    a.api_client = _ac()
    for k, val in kw.items():
        setattr(a, k, val)
    return a


def _ctx(
    *,
    redis=None,
    lab_unchained=False,
    god_mode=False,
    restart_rollout_explicit=False,
    inbound_proactive=False,
    telegram_chat_id=None,
    telegram=None,
    inbound_trace_id="test-trace",
    pre_action_state_revalidate_enabled=True,
    k8s_mutated=False,
):
    settings = SimpleNamespace(
        lab_unchained=lab_unchained,
        god_mode=god_mode,
        pre_action_state_revalidate_enabled=pre_action_state_revalidate_enabled,
    )
    ctx = SimpleNamespace(
        redis=redis,
        settings=settings,
        telegram=telegram,
        telegram_chat_id=telegram_chat_id,
        restart_rollout_explicit=restart_rollout_explicit,
        inbound_proactive=inbound_proactive,
        inbound_trace_id=inbound_trace_id,
        k8s_mutated=k8s_mutated,
    )
    return ctx


def _pod_item(name, ns="ns", phase="Running", ip="10.0.0.1"):
    p = MagicMock()
    p.metadata.name = name
    p.metadata.namespace = ns
    p.status.phase = phase
    p.status.pod_ip = ip
    return p


# ---------------------------------------------------------------------------
# resolve_pod_identity helpers (lines 87, 97-98, 124-133)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_pod_identity_empty_hint():
    from workers.k8s_tools import resolve_pod_identity

    v1 = _v1()
    result = await resolve_pod_identity(v1, "", None)
    assert result.kind == "not_found_cluster"


@pytest.mark.asyncio
async def test_resolve_pod_identity_with_ns_found():
    from workers.k8s_tools import resolve_pod_identity

    item = MagicMock()
    item.metadata.name = "my-pod-abc"
    lst = MagicMock()
    lst.items = [item]
    v1 = _v1(list_namespaced_pod=AsyncMock(return_value=lst))

    result = await resolve_pod_identity(v1, "my-pod", "ns")
    assert result.kind == "resolved"
    assert result.pod_name == "my-pod-abc"
    assert result.namespace == "ns"


@pytest.mark.asyncio
async def test_resolve_pod_identity_with_ns_not_found():
    from workers.k8s_tools import resolve_pod_identity

    lst = MagicMock()
    lst.items = []
    v1 = _v1(list_namespaced_pod=AsyncMock(return_value=lst))

    result = await resolve_pod_identity(v1, "ghost-pod", "ns")
    assert result.kind == "not_found_namespace"
    assert result.namespace == "ns"


@pytest.mark.asyncio
async def test_resolve_pod_identity_with_ns_404():
    from workers.k8s_tools import resolve_pod_identity

    exc = ApiException(status=404, reason="Not Found")
    v1 = _v1(list_namespaced_pod=AsyncMock(side_effect=exc))

    result = await resolve_pod_identity(v1, "pod", "bad-ns")
    assert result.kind == "not_found_namespace"


@pytest.mark.asyncio
async def test_resolve_pod_identity_cluster_scan_no_match():
    from workers.k8s_tools import resolve_pod_identity

    resp = MagicMock()
    resp.items = [_pod_item("some-other-pod")]
    v1 = _v1(list_pod_for_all_namespaces=AsyncMock(return_value=resp))

    result = await resolve_pod_identity(v1, "nonexistent", None)
    assert result.kind == "not_found_cluster"


@pytest.mark.asyncio
async def test_resolve_pod_identity_cluster_scan_ambiguous():
    from workers.k8s_tools import resolve_pod_identity

    resp = MagicMock()
    resp.items = [
        _pod_item("web-app-abc", ns="ns1"),
        _pod_item("web-app-def", ns="ns2"),
    ]
    v1 = _v1(list_pod_for_all_namespaces=AsyncMock(return_value=resp))

    result = await resolve_pod_identity(v1, "web-app", None)
    assert result.kind == "ambiguous"
    assert len(result.candidates) >= 2


@pytest.mark.asyncio
async def test_resolve_pod_identity_cluster_scan_single_match():
    from workers.k8s_tools import resolve_pod_identity

    resp = MagicMock()
    resp.items = [_pod_item("web-app-abc", ns="ns1")]
    v1 = _v1(list_pod_for_all_namespaces=AsyncMock(return_value=resp))

    result = await resolve_pod_identity(v1, "web-app", None)
    assert result.kind == "resolved"
    assert result.namespace == "ns1"


# ---------------------------------------------------------------------------
# resolve_deployment_identity (lines 120, 124-133)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_deployment_identity_empty_hint():
    from workers.k8s_tools import resolve_deployment_identity

    apps = _apps()
    result = await resolve_deployment_identity(apps, "", None)
    assert result.kind == "not_found_cluster"


@pytest.mark.asyncio
async def test_resolve_deployment_identity_with_ns_found():
    from workers.k8s_tools import resolve_deployment_identity

    item = MagicMock()
    item.metadata.name = "my-dep-xyz"
    lst = MagicMock()
    lst.items = [item]
    apps = _apps(list_namespaced_deployment=AsyncMock(return_value=lst))

    result = await resolve_deployment_identity(apps, "my-dep", "ns")
    assert result.kind == "resolved"
    assert result.deployment_name == "my-dep-xyz"


@pytest.mark.asyncio
async def test_resolve_deployment_identity_with_ns_not_found():
    from workers.k8s_tools import resolve_deployment_identity

    lst = MagicMock()
    lst.items = []
    apps = _apps(list_namespaced_deployment=AsyncMock(return_value=lst))

    result = await resolve_deployment_identity(apps, "ghost", "ns")
    assert result.kind == "not_found_namespace"


@pytest.mark.asyncio
async def test_resolve_deployment_identity_with_ns_404():
    from workers.k8s_tools import resolve_deployment_identity

    exc = ApiException(status=404, reason="Not Found")
    apps = _apps(list_namespaced_deployment=AsyncMock(side_effect=exc))

    result = await resolve_deployment_identity(apps, "dep", "bad-ns")
    assert result.kind == "not_found_namespace"


# ---------------------------------------------------------------------------
# deployment_evidence_snapshot (lines 188-189, 208)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deployment_evidence_snapshot_success():
    from workers.k8s_tools import deployment_evidence_snapshot

    dep = MagicMock()
    dep.metadata.generation = 5
    dep.metadata.resource_version = "100"
    dep.metadata.uid = "uid-abc"
    apps = _apps(read_namespaced_deployment=AsyncMock(return_value=dep))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        snap = await deployment_evidence_snapshot("ns", "dep")

    assert snap["deployment_generation"] == 5
    assert snap["deployment_resource_version"] == "100"
    assert snap["deployment_uid"] == "uid-abc"


@pytest.mark.asyncio
async def test_deployment_evidence_snapshot_api_error():
    from workers.k8s_tools import deployment_evidence_snapshot

    exc = ApiException(status=404, reason="Not Found")
    apps = _apps(read_namespaced_deployment=AsyncMock(side_effect=exc))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        snap = await deployment_evidence_snapshot("ns", "dep")

    assert snap == {}


# ---------------------------------------------------------------------------
# execute_rollout_restart (lines 215-216)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_rollout_restart_success():
    from workers.k8s_tools import execute_rollout_restart

    dep = MagicMock()
    dep.spec.template.metadata.annotations = {}
    apps = _apps(
        read_namespaced_deployment=AsyncMock(return_value=dep),
        replace_namespaced_deployment=AsyncMock(return_value=None),
    )

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await execute_rollout_restart("ns", "my-dep")

    assert "rollout_restart_ok" in result
    assert "restartedAt" in str(dep.spec.template.metadata.annotations)


@pytest.mark.asyncio
async def test_execute_rollout_restart_api_error():
    from workers.k8s_tools import execute_rollout_restart

    exc = ApiException(status=404, reason="Not Found")
    apps = _apps(
        read_namespaced_deployment=AsyncMock(side_effect=exc),
    )

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await execute_rollout_restart("ns", "missing")

    assert "api_error" in result


# ---------------------------------------------------------------------------
# execute_rollout_restart_from_pending (lines 230-243)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_rollout_restart_from_pending_missing_ns():
    from workers.k8s_tools import execute_rollout_restart_from_pending

    result = await execute_rollout_restart_from_pending(_ctx(), {"namespace": "", "deployment": "dep"})
    assert "Thiếu namespace" in result


@pytest.mark.asyncio
async def test_execute_rollout_restart_from_pending_missing_dep():
    from workers.k8s_tools import execute_rollout_restart_from_pending

    result = await execute_rollout_restart_from_pending(_ctx(), {"namespace": "ns", "deployment": ""})
    assert "Thiếu deployment" in result


@pytest.mark.asyncio
async def test_execute_rollout_restart_from_pending_no_snapshot_executes():
    from workers.k8s_tools import execute_rollout_restart_from_pending

    dep = MagicMock()
    dep.spec.template.metadata.annotations = {}
    apps = _apps(
        read_namespaced_deployment=AsyncMock(return_value=dep),
        replace_namespaced_deployment=AsyncMock(return_value=None),
    )

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await execute_rollout_restart_from_pending(
            _ctx(), {"namespace": "ns", "deployment": "dep"}
        )

    assert "rollout_restart_ok" in result


@pytest.mark.asyncio
async def test_execute_rollout_restart_from_pending_snapshot_mismatch_gen():
    from workers.k8s_tools import execute_rollout_restart_from_pending

    cur_dep = MagicMock()
    cur_dep.metadata.generation = 99
    cur_dep.metadata.resource_version = "rv"
    cur_dep.metadata.uid = "uid"
    apps = _apps(read_namespaced_deployment=AsyncMock(return_value=cur_dep))

    data = {
        "namespace": "ns",
        "deployment": "dep",
        "evidence_snapshot": {"deployment_generation": 1, "deployment_uid": "uid", "deployment_resource_version": "rv"},
    }

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await execute_rollout_restart_from_pending(_ctx(), data)

    assert "stale_state" in result
    assert "generation" in result


@pytest.mark.asyncio
async def test_execute_rollout_restart_from_pending_snapshot_uid_drift():
    from workers.k8s_tools import execute_rollout_restart_from_pending

    cur_dep = MagicMock()
    cur_dep.metadata.generation = 5
    cur_dep.metadata.resource_version = "rv"
    cur_dep.metadata.uid = "new-uid"
    apps = _apps(read_namespaced_deployment=AsyncMock(return_value=cur_dep))

    data = {
        "namespace": "ns",
        "deployment": "dep",
        "evidence_snapshot": {
            "deployment_generation": 5,
            "deployment_uid": "old-uid",
            "deployment_resource_version": "rv",
        },
    }

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await execute_rollout_restart_from_pending(_ctx(), data)

    assert "stale_state" in result
    assert "UID drift" in result or "uid" in result.lower()


@pytest.mark.asyncio
async def test_execute_rollout_restart_from_pending_stale_cur_empty():
    from workers.k8s_tools import execute_rollout_restart_from_pending

    exc = ApiException(status=404, reason="Not Found")
    apps = _apps(read_namespaced_deployment=AsyncMock(side_effect=exc))

    data = {
        "namespace": "ns",
        "deployment": "dep",
        "evidence_snapshot": {"deployment_generation": 1, "deployment_uid": "uid"},
    }

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await execute_rollout_restart_from_pending(_ctx(), data)

    assert "stale_state" in result


# ---------------------------------------------------------------------------
# _resolve_pod_name (line 303, 311, 317)
# ---------------------------------------------------------------------------


def test_resolve_pod_name_exact_match():
    from workers.k8s_tools import _resolve_pod_name

    items = [MagicMock() for _ in range(3)]
    items[0].metadata.name = "nginx-abc"
    items[1].metadata.name = "exact-pod"
    items[2].metadata.name = "other-abc"

    result = _resolve_pod_name("exact-pod", items)
    assert result == "exact-pod"


def test_resolve_pod_name_prefix_match():
    from workers.k8s_tools import _resolve_pod_name

    items = [MagicMock() for _ in range(2)]
    items[0].metadata.name = "nginx-abc-longer"
    items[1].metadata.name = "nginx-abc"

    result = _resolve_pod_name("nginx", items)
    # shortest prefix wins
    assert result == "nginx-abc"


def test_resolve_pod_name_substring_match():
    from workers.k8s_tools import _resolve_pod_name

    items = [MagicMock()]
    items[0].metadata.name = "my-nginx-pod"

    result = _resolve_pod_name("nginx", items)
    assert result == "my-nginx-pod"


def test_resolve_pod_name_no_match():
    from workers.k8s_tools import _resolve_pod_name

    items = [MagicMock()]
    items[0].metadata.name = "unrelated-pod"

    result = _resolve_pod_name("ghost", items)
    assert result is None


# ---------------------------------------------------------------------------
# _redis_pod_hints (lines 366-378)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_pod_hints_no_redis():
    from workers.k8s_tools import _redis_pod_hints

    ctx = SimpleNamespace()
    result = await _redis_pod_hints(ctx, "ns")
    assert result == ""


@pytest.mark.asyncio
async def test_redis_pod_hints_cache_hit():
    from workers.k8s_tools import _redis_pod_hints

    redis = MagicMock()
    redis.get = AsyncMock(return_value=json.dumps(["pod-a", "pod-b"]))
    ctx = SimpleNamespace(redis=redis)

    result = await _redis_pod_hints(ctx, "ns")
    assert "pod-a" in result
    assert "pod-b" in result


@pytest.mark.asyncio
async def test_redis_pod_hints_cache_miss():
    from workers.k8s_tools import _redis_pod_hints

    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    ctx = SimpleNamespace(redis=redis)

    result = await _redis_pod_hints(ctx, "ns")
    assert result == ""


@pytest.mark.asyncio
async def test_redis_pod_hints_redis_error_returns_empty():
    from workers.k8s_tools import _redis_pod_hints

    redis = MagicMock()
    redis.get = AsyncMock(side_effect=Exception("redis down"))
    ctx = SimpleNamespace(redis=redis)

    result = await _redis_pod_hints(ctx, "ns")
    assert result == ""


# ---------------------------------------------------------------------------
# tool_list_namespace_pods (lines 394-395)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_namespace_pods_no_namespace():
    from workers.k8s_tools import tool_list_namespace_pods

    result = await tool_list_namespace_pods(_ctx(), {"namespace": ""})
    assert "namespace" in result.lower()


@pytest.mark.asyncio
async def test_list_namespace_pods_none_namespace():
    from workers.k8s_tools import tool_list_namespace_pods

    result = await tool_list_namespace_pods(_ctx(), {})
    assert "namespace" in result.lower()


@pytest.mark.asyncio
async def test_list_namespace_pods_success_caches_to_redis():
    from workers.k8s_tools import tool_list_namespace_pods

    redis = MagicMock()
    redis.setex = AsyncMock()

    resp = MagicMock()
    resp.items = [_pod_item("pod-a"), _pod_item("pod-b")]
    v1 = _v1(list_namespaced_pod=AsyncMock(return_value=resp))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_list_namespace_pods(_ctx(redis=redis), {"namespace": "ns"})

    assert "pod-a" in result
    redis.setex.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_namespace_pods_api_error():
    from workers.k8s_tools import tool_list_namespace_pods

    exc = ApiException(status=403, reason="Forbidden")
    v1 = _v1(list_namespaced_pod=AsyncMock(side_effect=exc))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_list_namespace_pods(_ctx(), {"namespace": "ns"})

    assert "403" in result or "Lỗi" in result


@pytest.mark.asyncio
async def test_list_namespace_pods_generic_error():
    from workers.k8s_tools import tool_list_namespace_pods

    v1 = _v1(list_namespaced_pod=AsyncMock(side_effect=RuntimeError("boom")))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_list_namespace_pods(_ctx(), {"namespace": "ns"})

    assert "Lỗi" in result or "boom" in result


# ---------------------------------------------------------------------------
# tool_list_all_pods_sdk (lines 524, 528-529, 536-539, 558-559, 561)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_pods_sdk_no_pods():
    from workers.k8s_tools import tool_list_all_pods_sdk

    resp = MagicMock()
    resp.items = []
    v1 = _v1(list_pod_for_all_namespaces=AsyncMock(return_value=resp))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_list_all_pods_sdk(_ctx(), {})

    assert "empty" in result.lower() or "không" in result


@pytest.mark.asyncio
async def test_list_all_pods_sdk_with_pods_and_limit():
    from workers.k8s_tools import tool_list_all_pods_sdk

    resp = MagicMock()
    resp.items = [_pod_item(f"pod-{i}", ns="ns") for i in range(5)]
    v1 = _v1(list_pod_for_all_namespaces=AsyncMock(return_value=resp))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_list_all_pods_sdk(_ctx(), {"limit": 3})

    assert "pod-0" in result
    assert "+2 pod" in result or "2 pod" in result


@pytest.mark.asyncio
async def test_list_all_pods_sdk_api_error():
    from workers.k8s_tools import tool_list_all_pods_sdk

    exc = ApiException(status=403, reason="Forbidden")
    v1 = _v1(list_pod_for_all_namespaces=AsyncMock(side_effect=exc))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_list_all_pods_sdk(_ctx(), {})

    assert "error" in result.lower() and "403" in result


@pytest.mark.asyncio
async def test_list_all_pods_sdk_generic_error():
    from workers.k8s_tools import tool_list_all_pods_sdk

    v1 = _v1(list_pod_for_all_namespaces=AsyncMock(side_effect=RuntimeError("network fail")))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_list_all_pods_sdk(_ctx(), {})

    assert "error" in result.lower()


@pytest.mark.asyncio
async def test_list_all_pods_sdk_kubeconfig_error():
    from workers.k8s_tools import tool_list_all_pods_sdk

    with patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock, side_effect=Exception("no config")):
        result = await tool_list_all_pods_sdk(_ctx(), {})

    assert "kubeconfig" in result.lower() or "no config" in result


# ---------------------------------------------------------------------------
# tool_k8s_list_pods (lines 569-574)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_k8s_list_pods_with_namespace_delegates():
    from workers.k8s_tools import tool_k8s_list_pods

    resp = MagicMock()
    resp.items = [_pod_item("pod-a")]
    v1 = _v1(list_namespaced_pod=AsyncMock(return_value=resp))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_list_pods(_ctx(), {"namespace": "ns"})

    assert "pod-a" in result


@pytest.mark.asyncio
async def test_tool_k8s_list_pods_no_namespace_scans_cluster():
    from workers.k8s_tools import tool_k8s_list_pods

    resp = MagicMock()
    resp.items = [_pod_item("pod-a", ns="other-ns")]
    v1 = _v1(list_pod_for_all_namespaces=AsyncMock(return_value=resp))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_list_pods(_ctx(), {})

    assert "pod-a" in result


# ---------------------------------------------------------------------------
# tool_namespace_pods_top (lines 587-588, 614-615, 624, 628-631)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_namespace_pods_top_no_namespace():
    from workers.k8s_tools import tool_namespace_pods_top

    result = await tool_namespace_pods_top(_ctx(), {})
    assert "error" in result.lower()
    assert "namespace" in result.lower()


@pytest.mark.asyncio
async def test_namespace_pods_top_no_pods():
    from workers.k8s_tools import tool_namespace_pods_top

    resp = MagicMock()
    resp.items = []
    v1 = _v1(list_namespaced_pod=AsyncMock(return_value=resp))
    co = MagicMock()
    co.api_client = _ac()

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_tools.CustomObjectsApi", return_value=co),
    ):
        result = await tool_namespace_pods_top(_ctx(), {"namespace": "ns"})

    assert "empty" in result.lower() or "không" in result


@pytest.mark.asyncio
async def test_namespace_pods_top_with_metrics():
    from workers.k8s_tools import tool_namespace_pods_top

    resp = MagicMock()
    resp.items = [_pod_item("pod-a")]
    v1 = _v1(list_namespaced_pod=AsyncMock(return_value=resp))

    mbody = {
        "containers": [{"usage": {"cpu": "100m", "memory": "128Mi"}}]
    }
    co = MagicMock()
    co.api_client = _ac()
    co.get_namespaced_custom_object = AsyncMock(return_value=mbody)

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_tools.CustomObjectsApi", return_value=co),
    ):
        result = await tool_namespace_pods_top(_ctx(), {"namespace": "ns"})

    assert "pod-a" in result
    assert "cpu_cores" in result


@pytest.mark.asyncio
async def test_namespace_pods_top_metrics_404():
    from workers.k8s_tools import tool_namespace_pods_top

    resp = MagicMock()
    resp.items = [_pod_item("pod-a")]
    v1 = _v1(list_namespaced_pod=AsyncMock(return_value=resp))

    exc = ApiException(status=404, reason="Not Found")
    co = MagicMock()
    co.api_client = _ac()
    co.get_namespaced_custom_object = AsyncMock(side_effect=exc)

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_tools.CustomObjectsApi", return_value=co),
    ):
        result = await tool_namespace_pods_top(_ctx(), {"namespace": "ns"})

    assert "n/a" in result


@pytest.mark.asyncio
async def test_namespace_pods_top_api_error():
    from workers.k8s_tools import tool_namespace_pods_top

    exc = ApiException(status=403, reason="Forbidden")
    v1 = _v1(list_namespaced_pod=AsyncMock(side_effect=exc))
    co = MagicMock()
    co.api_client = _ac()

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_tools.CustomObjectsApi", return_value=co),
    ):
        result = await tool_namespace_pods_top(_ctx(), {"namespace": "ns"})

    assert "error" in result.lower() and "403" in result


# ---------------------------------------------------------------------------
# tool_resolve_pod_identity (lines 650-651, 669-676, 684-687)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_pod_identity_tool_no_hint():
    from workers.k8s_tools import tool_resolve_pod_identity

    result = await tool_resolve_pod_identity(_ctx(), {})
    assert "Thiếu" in result or "Missing" in result or "hint" in result.lower()


@pytest.mark.asyncio
async def test_resolve_pod_identity_tool_ambiguous():
    from workers.k8s_tools import tool_resolve_pod_identity

    resp = MagicMock()
    resp.items = [
        _pod_item("web-app-abc", ns="ns1"),
        _pod_item("web-app-def", ns="ns2"),
    ]
    v1 = _v1(list_pod_for_all_namespaces=AsyncMock(return_value=resp))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_resolve_pod_identity(_ctx(), {"pod_name": "web-app"})

    assert "ambiguous" in result.lower()


@pytest.mark.asyncio
async def test_resolve_pod_identity_tool_not_found_cluster():
    from workers.k8s_tools import tool_resolve_pod_identity

    resp = MagicMock()
    resp.items = [_pod_item("other-pod")]
    v1 = _v1(list_pod_for_all_namespaces=AsyncMock(return_value=resp))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_resolve_pod_identity(_ctx(), {"pod_name": "ghost-pod"})

    assert "not_found" in result.lower() or "không thấy" in result


@pytest.mark.asyncio
async def test_resolve_pod_identity_tool_not_found_namespace():
    from workers.k8s_tools import tool_resolve_pod_identity

    lst = MagicMock()
    lst.items = []
    v1 = _v1(list_namespaced_pod=AsyncMock(return_value=lst))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_resolve_pod_identity(_ctx(), {"pod_name": "ghost", "namespace": "ns"})

    assert "not_found" in result.lower() or "không thấy" in result


@pytest.mark.asyncio
async def test_resolve_pod_identity_tool_resolved():
    from workers.k8s_tools import tool_resolve_pod_identity

    resp = MagicMock()
    resp.items = [_pod_item("web-app-abc", ns="ns1")]
    v1 = _v1(list_pod_for_all_namespaces=AsyncMock(return_value=resp))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_resolve_pod_identity(_ctx(), {"pod_name": "web-app"})

    assert "resolved_pod" in result
    assert "ns1" in result


@pytest.mark.asyncio
async def test_resolve_pod_identity_tool_api_error():
    from workers.k8s_tools import tool_resolve_pod_identity

    exc = ApiException(status=403, reason="Forbidden")
    v1 = _v1(list_pod_for_all_namespaces=AsyncMock(side_effect=exc))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_resolve_pod_identity(_ctx(), {"pod_name": "pod"})

    assert "api_error" in result or "error" in result.lower()


@pytest.mark.asyncio
async def test_resolve_pod_identity_tool_kubeconfig_error():
    from workers.k8s_tools import tool_resolve_pod_identity

    with patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock, side_effect=Exception("no config")):
        result = await tool_resolve_pod_identity(_ctx(), {"pod_name": "pod"})

    assert "kubeconfig" in result.lower() or "no config" in result


# ---------------------------------------------------------------------------
# tool_resolve_deployment_identity (lines 705-706, 712-713, 724, 735-738)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_deployment_identity_tool_no_hint():
    from workers.k8s_tools import tool_resolve_deployment_identity

    result = await tool_resolve_deployment_identity(_ctx(), {})
    assert "Thiếu" in result or "hint" in result.lower()


@pytest.mark.asyncio
async def test_resolve_deployment_identity_tool_ambiguous():
    from workers.k8s_tools import tool_resolve_deployment_identity

    dep1 = MagicMock()
    dep1.metadata.name = "web-dep-prod"
    dep1.metadata.namespace = "prod"
    dep2 = MagicMock()
    dep2.metadata.name = "web-dep-stage"
    dep2.metadata.namespace = "stage"
    resp = MagicMock()
    resp.items = [dep1, dep2]
    apps = _apps(list_deployment_for_all_namespaces=AsyncMock(return_value=resp))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_resolve_deployment_identity(_ctx(), {"deployment": "web-dep"})

    assert "ambiguous" in result.lower()


@pytest.mark.asyncio
async def test_resolve_deployment_identity_tool_not_found_cluster():
    from workers.k8s_tools import tool_resolve_deployment_identity

    resp = MagicMock()
    resp.items = []
    apps = _apps(list_deployment_for_all_namespaces=AsyncMock(return_value=resp))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_resolve_deployment_identity(_ctx(), {"deployment": "ghost"})

    assert "not_found" in result.lower() or "không" in result


@pytest.mark.asyncio
async def test_resolve_deployment_identity_tool_not_found_namespace():
    from workers.k8s_tools import tool_resolve_deployment_identity

    lst = MagicMock()
    lst.items = []
    apps = _apps(list_namespaced_deployment=AsyncMock(return_value=lst))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_resolve_deployment_identity(_ctx(), {"deployment": "ghost", "namespace": "ns"})

    assert "not_found" in result.lower() or "không" in result


@pytest.mark.asyncio
async def test_resolve_deployment_identity_tool_resolved():
    from workers.k8s_tools import tool_resolve_deployment_identity

    dep = MagicMock()
    dep.metadata.name = "web-dep-abc"
    dep.metadata.namespace = "prod"
    resp = MagicMock()
    resp.items = [dep]
    apps = _apps(list_deployment_for_all_namespaces=AsyncMock(return_value=resp))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_resolve_deployment_identity(_ctx(), {"deployment": "web-dep"})

    assert "resolved_deployment" in result
    assert "prod" in result


@pytest.mark.asyncio
async def test_resolve_deployment_identity_tool_api_error():
    from workers.k8s_tools import tool_resolve_deployment_identity

    exc = ApiException(status=403, reason="Forbidden")
    apps = _apps(list_deployment_for_all_namespaces=AsyncMock(side_effect=exc))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_resolve_deployment_identity(_ctx(), {"deployment": "dep"})

    assert "api_error" in result or "error" in result.lower()


# ---------------------------------------------------------------------------
# tool_inspect_pod_deep (lines 756-789, 821-825, 845-848, 861, 863, 870-877)
# ---------------------------------------------------------------------------


def _full_pod_mock(name="mypod", phase="Running", restarts=0):
    pod = MagicMock()
    pod.status.phase = phase
    pod.status.pod_ip = "10.0.0.1"
    cs = MagicMock()
    cs.restart_count = restarts
    cs.ready = True
    pod.status.container_statuses = [cs]
    c = MagicMock()
    c.name = "main"
    c.resources.limits = {"cpu": "500m", "memory": "256Mi"}
    c.resources.requests = {}
    pod.spec.containers = [c]
    pod.metadata.name = name
    return pod


@pytest.mark.asyncio
async def test_inspect_pod_deep_no_hint():
    from workers.k8s_tools import tool_inspect_pod_deep

    result = await tool_inspect_pod_deep(_ctx(), {})
    assert "Missing" in result or "pod_name" in result


@pytest.mark.asyncio
async def test_inspect_pod_deep_not_found_cluster():
    from workers.k8s_tools import tool_inspect_pod_deep

    resp = MagicMock()
    resp.items = [_pod_item("other-pod")]
    v1 = _v1(
        list_pod_for_all_namespaces=AsyncMock(return_value=resp),
    )
    co = MagicMock()
    co.api_client = _ac()

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_tools.CustomObjectsApi", return_value=co),
    ):
        result = await tool_inspect_pod_deep(_ctx(), {"pod_name": "ghost"})

    assert "not_found" in result.lower() or "No pod" in result


@pytest.mark.asyncio
async def test_inspect_pod_deep_not_found_namespace():
    from workers.k8s_tools import tool_inspect_pod_deep

    lst = MagicMock()
    lst.items = []
    v1 = _v1(list_namespaced_pod=AsyncMock(return_value=lst))
    co = MagicMock()
    co.api_client = _ac()

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_tools.CustomObjectsApi", return_value=co),
    ):
        result = await tool_inspect_pod_deep(_ctx(), {"pod_name": "ghost", "namespace": "ns"})

    assert "not_found" in result.lower() or "No matching" in result


@pytest.mark.asyncio
async def test_inspect_pod_deep_kubeconfig_error():
    from workers.k8s_tools import tool_inspect_pod_deep

    with patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock, side_effect=Exception("no kubeconfig")):
        result = await tool_inspect_pod_deep(_ctx(), {"pod_name": "pod"})

    assert "kubeconfig" in result.lower() or "no kubeconfig" in result


@pytest.mark.asyncio
async def test_inspect_pod_deep_success_with_metrics():
    from workers.k8s_tools import tool_inspect_pod_deep

    pod = _full_pod_mock(name="mypod", phase="Running", restarts=0)
    resp = MagicMock()
    resp.items = [_pod_item("mypod", ns="ns")]
    ev_resp = MagicMock()
    ev_resp.items = []

    v1 = _v1(
        list_pod_for_all_namespaces=AsyncMock(return_value=resp),
        read_namespaced_pod=AsyncMock(return_value=pod),
        read_namespaced_pod_log=AsyncMock(return_value="log line 1\nlog line 2"),
        list_namespaced_event=AsyncMock(return_value=ev_resp),
    )
    mbody = {"containers": [{"usage": {"cpu": "100m", "memory": "64Mi"}}]}
    co = MagicMock()
    co.api_client = _ac()
    co.get_namespaced_custom_object = AsyncMock(return_value=mbody)

    tg = MagicMock()
    tg.send_photo_bytes = AsyncMock()

    mock_png = b"PNGDATA"

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_tools.CustomObjectsApi", return_value=co),
        patch("workers.k8s_tools.pod_cpu_memory_bar_png_bytes", return_value=mock_png),
        patch("workers.k8s_tools.pod_cpu_memory_usage_absolute_png_bytes", return_value=mock_png),
        patch("workers.k8s_tools.should_send_telegram_chart", return_value=False),
        patch("workers.k8s_tools.effective_telegram_chat_id", return_value=None),
    ):
        result = await tool_inspect_pod_deep(_ctx(telegram=tg), {"pod_name": "mypod"})

    assert "[DATA]" in result
    assert "mypod" in result
    assert "Running" in result


@pytest.mark.asyncio
async def test_inspect_pod_deep_high_restarts_diag():
    from workers.k8s_tools import tool_inspect_pod_deep

    pod = _full_pod_mock(name="crashpod", phase="Running", restarts=10)
    resp = MagicMock()
    resp.items = [_pod_item("crashpod", ns="ns")]
    ev_resp = MagicMock()
    ev_resp.items = []

    v1 = _v1(
        list_pod_for_all_namespaces=AsyncMock(return_value=resp),
        read_namespaced_pod=AsyncMock(return_value=pod),
        read_namespaced_pod_log=AsyncMock(return_value="crash"),
        list_namespaced_event=AsyncMock(return_value=ev_resp),
    )
    exc = ApiException(status=404, reason="Not Found")
    co = MagicMock()
    co.api_client = _ac()
    co.get_namespaced_custom_object = AsyncMock(side_effect=exc)

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_tools.CustomObjectsApi", return_value=co),
        patch("workers.k8s_tools.pod_cpu_memory_bar_png_bytes", return_value=b"png"),
        patch("workers.k8s_tools.pod_cpu_memory_usage_absolute_png_bytes", return_value=b"png"),
        patch("workers.k8s_tools.should_send_telegram_chart", return_value=False),
        patch("workers.k8s_tools.effective_telegram_chat_id", return_value=None),
    ):
        result = await tool_inspect_pod_deep(_ctx(), {"pod_name": "crashpod"})

    assert "restart" in result.lower() or "10" in result


@pytest.mark.asyncio
async def test_inspect_pod_deep_warning_events():
    from workers.k8s_tools import tool_inspect_pod_deep

    pod = _full_pod_mock(name="warnpod", phase="Running", restarts=0)
    resp = MagicMock()
    resp.items = [_pod_item("warnpod", ns="ns")]

    ev = MagicMock()
    ev.type = "Warning"
    ev.reason = "OOMKilled"
    ev.message = "container OOMKilled"
    ev.last_timestamp = None
    ev.event_time = None
    ev_resp = MagicMock()
    ev_resp.items = [ev]

    v1 = _v1(
        list_pod_for_all_namespaces=AsyncMock(return_value=resp),
        read_namespaced_pod=AsyncMock(return_value=pod),
        read_namespaced_pod_log=AsyncMock(return_value="log"),
        list_namespaced_event=AsyncMock(return_value=ev_resp),
    )
    exc = ApiException(status=404, reason="Not Found")
    co = MagicMock()
    co.api_client = _ac()
    co.get_namespaced_custom_object = AsyncMock(side_effect=exc)

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_tools.CustomObjectsApi", return_value=co),
        patch("workers.k8s_tools.pod_cpu_memory_bar_png_bytes", return_value=b"png"),
        patch("workers.k8s_tools.pod_cpu_memory_usage_absolute_png_bytes", return_value=b"png"),
        patch("workers.k8s_tools.should_send_telegram_chart", return_value=False),
        patch("workers.k8s_tools.effective_telegram_chat_id", return_value=None),
    ):
        result = await tool_inspect_pod_deep(_ctx(), {"pod_name": "warnpod"})

    assert "OOMKilled" in result or "Warning" in result


@pytest.mark.asyncio
async def test_inspect_pod_deep_sends_telegram():
    from workers.k8s_tools import tool_inspect_pod_deep

    pod = _full_pod_mock(name="mypod", phase="Running", restarts=0)
    resp = MagicMock()
    resp.items = [_pod_item("mypod", ns="ns")]
    ev_resp = MagicMock()
    ev_resp.items = []

    v1 = _v1(
        list_pod_for_all_namespaces=AsyncMock(return_value=resp),
        read_namespaced_pod=AsyncMock(return_value=pod),
        read_namespaced_pod_log=AsyncMock(return_value="log"),
        list_namespaced_event=AsyncMock(return_value=ev_resp),
    )
    exc = ApiException(status=404, reason="Not Found")
    co = MagicMock()
    co.api_client = _ac()
    co.get_namespaced_custom_object = AsyncMock(side_effect=exc)

    tg = MagicMock()
    tg.send_photo_bytes = AsyncMock()
    mock_png = b"PNGDATA"

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_tools.CustomObjectsApi", return_value=co),
        patch("workers.k8s_tools.pod_cpu_memory_bar_png_bytes", return_value=mock_png),
        patch("workers.k8s_tools.pod_cpu_memory_usage_absolute_png_bytes", return_value=mock_png),
        patch("workers.k8s_tools.should_send_telegram_chart", return_value=True),
        patch("workers.k8s_tools.effective_telegram_chat_id", return_value=12345),
    ):
        result = await tool_inspect_pod_deep(
            _ctx(telegram=tg, telegram_chat_id=12345), {"pod_name": "mypod"}
        )

    assert "telegram_photo" in result or "sent" in result
    tg.send_photo_bytes.assert_awaited_once()


@pytest.mark.asyncio
async def test_inspect_pod_deep_ambiguous():
    from workers.k8s_tools import tool_inspect_pod_deep

    resp = MagicMock()
    resp.items = [
        _pod_item("web-abc", ns="ns1"),
        _pod_item("web-def", ns="ns2"),
    ]
    v1 = _v1(list_pod_for_all_namespaces=AsyncMock(return_value=resp))
    co = MagicMock()
    co.api_client = _ac()

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_tools.CustomObjectsApi", return_value=co),
    ):
        result = await tool_inspect_pod_deep(_ctx(), {"pod_name": "web"})

    assert "ambiguous" in result.lower()


# ---------------------------------------------------------------------------
# tool_k8s_rollout_restart (lines 970-971, 984, 989, 991-992, 1021-1022)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_k8s_rollout_restart_no_name():
    from workers.k8s_tools import tool_k8s_rollout_restart

    result = await tool_k8s_rollout_restart(_ctx(), {})
    assert "Thiếu" in result or "deployment" in result.lower()


@pytest.mark.asyncio
async def test_tool_k8s_rollout_restart_with_explicit_ns_found_explicit_user():
    from workers.k8s_tools import tool_k8s_rollout_restart

    dep = MagicMock()
    dep.spec.template.metadata.annotations = {}
    apps = _apps(
        read_namespaced_deployment=AsyncMock(return_value=dep),
        replace_namespaced_deployment=AsyncMock(return_value=None),
    )

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_rollout_restart(
            _ctx(restart_rollout_explicit=True), {"deployment": "dep", "namespace": "ns"}
        )

    assert "rollout_restart_ok" in result


@pytest.mark.asyncio
async def test_tool_k8s_rollout_restart_with_explicit_ns_not_found():
    from workers.k8s_tools import tool_k8s_rollout_restart

    exc = ApiException(status=404, reason="Not Found")
    apps = _apps(read_namespaced_deployment=AsyncMock(side_effect=exc))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_rollout_restart(_ctx(), {"deployment": "dep", "namespace": "ns"})

    assert "deployment_not_found" in result or "Không có" in result


@pytest.mark.asyncio
async def test_tool_k8s_rollout_restart_with_explicit_ns_api_error():
    from workers.k8s_tools import tool_k8s_rollout_restart

    exc = ApiException(status=403, reason="Forbidden")
    apps = _apps(read_namespaced_deployment=AsyncMock(side_effect=exc))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_rollout_restart(_ctx(), {"deployment": "dep", "namespace": "ns"})

    assert "api_error" in result


@pytest.mark.asyncio
async def test_tool_k8s_rollout_restart_cluster_scan_not_found():
    from workers.k8s_tools import tool_k8s_rollout_restart

    resp = MagicMock()
    resp.items = []
    apps = _apps(list_deployment_for_all_namespaces=AsyncMock(return_value=resp))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_rollout_restart(_ctx(), {"deployment": "ghost"})

    assert "deployment_not_found" in result or "không tìm" in result.lower()


@pytest.mark.asyncio
async def test_tool_k8s_rollout_restart_cluster_scan_ambiguous():
    from workers.k8s_tools import tool_k8s_rollout_restart

    dep1 = MagicMock()
    dep1.metadata.name = "web-dep-prod"
    dep1.metadata.namespace = "prod"
    dep2 = MagicMock()
    dep2.metadata.name = "web-dep-stage"
    dep2.metadata.namespace = "stage"
    resp = MagicMock()
    resp.items = [dep1, dep2]
    apps = _apps(list_deployment_for_all_namespaces=AsyncMock(return_value=resp))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_rollout_restart(_ctx(), {"deployment": "web-dep"})

    assert "ambiguous" in result.lower()


@pytest.mark.asyncio
async def test_tool_k8s_rollout_restart_proactive_executes():
    from workers.k8s_tools import tool_k8s_rollout_restart

    dep = MagicMock()
    dep.spec.template.metadata.annotations = {}
    apps = _apps(
        read_namespaced_deployment=AsyncMock(return_value=dep),
        replace_namespaced_deployment=AsyncMock(return_value=None),
    )

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_rollout_restart(
            _ctx(inbound_proactive=True), {"deployment": "dep", "namespace": "ns"}
        )

    assert "rollout_restart_ok" in result


@pytest.mark.asyncio
async def test_tool_k8s_rollout_restart_no_chat_requires_confirm():
    from workers.k8s_tools import tool_k8s_rollout_restart

    dep = MagicMock()
    apps = _apps(read_namespaced_deployment=AsyncMock(return_value=dep))

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_rollout_restart(
            _ctx(telegram_chat_id=None, restart_rollout_explicit=False),
            {"deployment": "dep", "namespace": "ns"},
        )

    assert "confirm" in result.lower() or "CONFIRM" in result


@pytest.mark.asyncio
async def test_tool_k8s_rollout_restart_redis_pending_written():
    from workers.k8s_tools import tool_k8s_rollout_restart

    redis = MagicMock()
    redis.setex = AsyncMock()

    dep_snap = MagicMock()
    dep_snap.metadata.generation = 3
    dep_snap.metadata.resource_version = "rv"
    dep_snap.metadata.uid = "uid"
    apps = _apps(
        read_namespaced_deployment=AsyncMock(return_value=dep_snap),
    )

    with (
        patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_rollout_restart(
            _ctx(redis=redis, telegram_chat_id=12345, restart_rollout_explicit=False),
            {"deployment": "dep", "namespace": "ns"},
        )

    assert "CONFIRM" in result or "confirm" in result.lower()
    redis.setex.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_k8s_rollout_restart_kubeconfig_error():
    from workers.k8s_tools import tool_k8s_rollout_restart

    with patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock, side_effect=Exception("no kubeconfig")):
        result = await tool_k8s_rollout_restart(_ctx(), {"deployment": "dep"})

    assert "kubeconfig" in result.lower() or "no kubeconfig" in result


# ---------------------------------------------------------------------------
# tool_inspect_pod_details alias (line 1035-1037)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_pod_details_alias_no_hint():
    from workers.k8s_tools import tool_inspect_pod_details

    result = await tool_inspect_pod_details(_ctx(), {})
    assert "Missing" in result or "pod_name" in result


# ---------------------------------------------------------------------------
# Pure helper functions (no k8s needed)
# ---------------------------------------------------------------------------


def test_discover_pairs_from_hint_exact():
    from workers.k8s_tools import _discover_pairs_from_hint

    pairs = [("ns", "nginx-abc"), ("ns", "redis-xyz"), ("ns", "exact-match")]
    result = _discover_pairs_from_hint("exact-match", pairs)
    assert result == [("ns", "exact-match")]


def test_discover_pairs_from_hint_prefix():
    from workers.k8s_tools import _discover_pairs_from_hint

    pairs = [("ns", "nginx-abc-longer"), ("ns", "nginx-abc")]
    result = _discover_pairs_from_hint("nginx", pairs)
    assert result[0][1] == "nginx-abc"  # shortest first


def test_discover_pairs_from_hint_substring():
    from workers.k8s_tools import _discover_pairs_from_hint

    pairs = [("ns", "my-nginx-pod")]
    result = _discover_pairs_from_hint("nginx", pairs)
    assert result[0][1] == "my-nginx-pod"


def test_discover_pairs_from_hint_no_match():
    from workers.k8s_tools import _discover_pairs_from_hint

    pairs = [("ns", "redis-abc")]
    result = _discover_pairs_from_hint("postgres", pairs)
    assert result == []


def test_ws_allows_kubectl_list_all_no_settings():
    from workers.k8s_tools import _ws_allows_kubectl_list_all

    ctx = SimpleNamespace()
    assert _ws_allows_kubectl_list_all(ctx) is False


def test_ws_allows_kubectl_list_all_lab_unchained():
    from workers.k8s_tools import _ws_allows_kubectl_list_all

    ctx = SimpleNamespace(settings=SimpleNamespace(lab_unchained=True, god_mode=False))
    assert _ws_allows_kubectl_list_all(ctx) is True


def test_ws_allows_kubectl_list_all_god_mode():
    from workers.k8s_tools import _ws_allows_kubectl_list_all

    ctx = SimpleNamespace(settings=SimpleNamespace(lab_unchained=False, god_mode=True))
    assert _ws_allows_kubectl_list_all(ctx) is True


def test_parse_kubectl_get_pods_lines():
    from workers.k8s_tools import _parse_kubectl_get_pods_lines

    lines = [
        "NAMESPACE    NAME           READY   STATUS    RESTARTS   AGE",
        "default      nginx-abc      1/1     Running   0          5d",
        "kube-system  coredns-xyz    1/1     Running   2          10d",
        "",
    ]
    pairs, body = _parse_kubectl_get_pods_lines(lines)
    assert len(pairs) == 2
    assert pairs[0] == ("default", "nginx-abc")
    assert pairs[1] == ("kube-system", "coredns-xyz")
    assert len(body) == 2


def test_format_pod_list():
    from workers.k8s_tools import _format_pod_list

    pod = MagicMock()
    pod.metadata.name = "my-pod"
    pod.status.phase = "Running"
    pod.status.pod_ip = "10.0.0.5"
    resp = MagicMock()
    resp.items = [pod]

    result = _format_pod_list(resp, "ns")
    assert "my-pod" in result
    assert "Running" in result
    assert "10.0.0.5" in result


def test_format_pod_list_empty():
    from workers.k8s_tools import _format_pod_list

    resp = MagicMock()
    resp.items = []

    result = _format_pod_list(resp, "ns")
    assert "không có pod" in result


def test_cpu_to_cores_millis():
    from workers.k8s_tools import _cpu_to_cores

    assert abs(_cpu_to_cores("500m") - 0.5) < 1e-9


def test_cpu_to_cores_nanos():
    from workers.k8s_tools import _cpu_to_cores

    assert abs(_cpu_to_cores("1000000000n") - 1.0) < 1e-6


def test_cpu_to_cores_full():
    from workers.k8s_tools import _cpu_to_cores

    assert abs(_cpu_to_cores("2") - 2.0) < 1e-9


def test_cpu_to_cores_none():
    from workers.k8s_tools import _cpu_to_cores

    assert _cpu_to_cores(None) == 0.0


def test_mem_to_bytes_mi():
    from workers.k8s_tools import _mem_to_bytes

    assert _mem_to_bytes("128Mi") == 128 * 1024 * 1024


def test_mem_to_bytes_gi():
    from workers.k8s_tools import _mem_to_bytes

    assert _mem_to_bytes("1Gi") == 1024 ** 3


def test_mem_to_bytes_ki():
    from workers.k8s_tools import _mem_to_bytes

    assert _mem_to_bytes("4Ki") == 4096


def test_mem_to_bytes_k():
    from workers.k8s_tools import _mem_to_bytes

    assert _mem_to_bytes("1K") == 1000


def test_mem_to_bytes_digit():
    from workers.k8s_tools import _mem_to_bytes

    assert _mem_to_bytes("1024") == 1024


def test_mem_to_bytes_none():
    from workers.k8s_tools import _mem_to_bytes

    assert _mem_to_bytes(None) == 0


def test_pct_zero_cap():
    from workers.k8s_tools import _pct

    assert _pct(100.0, 0.0) == 0.0


def test_pct_normal():
    from workers.k8s_tools import _pct

    assert abs(_pct(50.0, 100.0) - 50.0) < 1e-9


def test_pct_over_100_clamped():
    from workers.k8s_tools import _pct

    assert _pct(200.0, 100.0) == 100.0


def test_event_is_warning_or_critical_warning_type():
    from workers.k8s_tools import _event_is_warning_or_critical

    e = SimpleNamespace(type="Warning", reason="SomeReason")
    assert _event_is_warning_or_critical(e) is True


def test_event_is_warning_or_critical_oom():
    from workers.k8s_tools import _event_is_warning_or_critical

    e = SimpleNamespace(type="Normal", reason="OOMKilled")
    assert _event_is_warning_or_critical(e) is True


def test_event_is_warning_or_critical_normal():
    from workers.k8s_tools import _event_is_warning_or_critical

    e = SimpleNamespace(type="Normal", reason="Scheduled")
    assert _event_is_warning_or_critical(e) is False
