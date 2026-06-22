"""
tests/test_cov_k8s_cluster_tools.py

Coverage-gap tests for src/workers/k8s_cluster_tools.py
Targets uncovered lines: 74-90, 116-120, 138-139, 169-186, 196-217, 238-258,
284-304, 339-443, 472-499, 532-563, 579-599, 610-635, 648-679, 700-709,
728-742, 774-775, 787-791, 818-841, 851-880, 906-922, 928, 932, 946,
966-967, 1000-1001, 1030-1057, 1069-1097

All K8s API calls are mocked; no cluster connection is required.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client import ApiException


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _api_client():
    ac = MagicMock()
    ac.close = AsyncMock()
    return ac


def _v1(**kwargs):
    v = MagicMock()
    v.api_client = _api_client()
    for k, val in kwargs.items():
        setattr(v, k, val)
    return v


def _apps(**kwargs):
    a = MagicMock()
    a.api_client = _api_client()
    for k, val in kwargs.items():
        setattr(a, k, val)
    return a


def _rbac(**kwargs):
    r = MagicMock()
    r.api_client = _api_client()
    for k, val in kwargs.items():
        setattr(r, k, val)
    return r


def _ev_list(n=0):
    ev = MagicMock()
    ev.items = [MagicMock()] * n
    return ev


def _dep_mock(replicas=1, ready=1, available=1, unavailable=0, updated=1,
              observed_gen=1, generation=1):
    dep = MagicMock()
    dep.spec.replicas = replicas
    dep.status.ready_replicas = ready
    dep.status.available_replicas = available
    dep.status.unavailable_replicas = unavailable
    dep.status.updated_replicas = updated
    dep.status.observed_generation = observed_gen
    dep.metadata.generation = generation
    return dep


# ---------------------------------------------------------------------------
# tool_k8s_scale_deployment  (lines 73-90)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_deployment_success():
    from workers.k8s_cluster_tools import ScaleDeploymentArgs, tool_k8s_scale_deployment

    dep = MagicMock()
    dep.spec.replicas = 1
    apps = _apps(
        read_namespaced_deployment=AsyncMock(return_value=dep),
        replace_namespaced_deployment=AsyncMock(return_value=None),
    )
    args = ScaleDeploymentArgs(name="my-dep", namespace="ns", replicas=3, reasoning="scale up for load")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_scale_deployment(None, args)

    assert "scale_ok" in result
    assert "my-dep" in result
    assert "replicas=3" in result
    assert "scale up for load" in result


@pytest.mark.asyncio
async def test_scale_deployment_no_reasoning():
    from workers.k8s_cluster_tools import ScaleDeploymentArgs, tool_k8s_scale_deployment

    dep = MagicMock()
    dep.spec.replicas = 1
    apps = _apps(
        read_namespaced_deployment=AsyncMock(return_value=dep),
        replace_namespaced_deployment=AsyncMock(return_value=None),
    )
    args = ScaleDeploymentArgs(name="dep", namespace="ns", replicas=0)

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_scale_deployment(None, args)

    assert "scale_ok" in result
    assert "reasoning=" not in result


@pytest.mark.asyncio
async def test_scale_deployment_api_error():
    from workers.k8s_cluster_tools import ScaleDeploymentArgs, tool_k8s_scale_deployment

    exc = ApiException(status=403, reason="Forbidden")
    apps = _apps(
        read_namespaced_deployment=AsyncMock(side_effect=exc),
    )
    args = ScaleDeploymentArgs(name="dep", namespace="ns", replicas=2)

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_scale_deployment(None, args)

    assert "[DATA] error" in result


# ---------------------------------------------------------------------------
# tool_k8s_describe_resource — Pod branch (line 116-117)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_pod_success():
    from workers.k8s_cluster_tools import DescribeResourceArgs, tool_k8s_describe_resource

    pod_obj = MagicMock()
    pod_obj.to_dict.return_value = {"metadata": {"name": "my-pod"}}
    v1 = _v1(
        read_namespaced_pod=AsyncMock(return_value=pod_obj),
        list_namespaced_event=AsyncMock(return_value=_ev_list(2)),
    )
    apps = _apps()

    args = DescribeResourceArgs(resource_type="Pod", name="my-pod", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_describe_resource(None, args)

    assert "describe_ok" in result
    assert "kind=Pod" in result
    assert "events_n=2" in result


# ---------------------------------------------------------------------------
# tool_k8s_describe_resource — Deployment branch (line 119-120)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_deployment_success():
    from workers.k8s_cluster_tools import DescribeResourceArgs, tool_k8s_describe_resource

    dep_obj = MagicMock()
    dep_obj.to_dict.return_value = {"metadata": {"name": "my-dep"}}
    v1 = _v1(
        list_namespaced_event=AsyncMock(return_value=_ev_list(0)),
    )
    apps = _apps(
        read_namespaced_deployment=AsyncMock(return_value=dep_obj),
    )

    args = DescribeResourceArgs(resource_type="Deployment", name="my-dep", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_describe_resource(None, args)

    assert "describe_ok" in result
    assert "kind=Deployment" in result


# ---------------------------------------------------------------------------
# tool_k8s_describe_resource — Service branch (line 138-139)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_service_success():
    from workers.k8s_cluster_tools import DescribeResourceArgs, tool_k8s_describe_resource

    svc_obj = MagicMock()
    svc_obj.to_dict.return_value = {"metadata": {"name": "my-svc"}}
    v1 = _v1(
        read_namespaced_service=AsyncMock(return_value=svc_obj),
        list_namespaced_event=AsyncMock(return_value=_ev_list(1)),
    )
    apps = _apps()

    args = DescribeResourceArgs(resource_type="Service", name="my-svc", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_describe_resource(None, args)

    assert "describe_ok" in result
    assert "kind=Service" in result


# ---------------------------------------------------------------------------
# tool_k8s_tail_logs (lines 169-186)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tail_logs_success():
    from workers.k8s_cluster_tools import TailLogsArgs, tool_k8s_tail_logs

    v1 = _v1(
        read_namespaced_pod_log=AsyncMock(return_value="line1\nline2\n"),
    )
    args = TailLogsArgs(pod_name="mypod", namespace="ns", lines=50)

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_tail_logs(None, args)

    assert "logs_ok" in result
    assert "line1" in result


@pytest.mark.asyncio
async def test_tail_logs_empty_log():
    from workers.k8s_cluster_tools import TailLogsArgs, tool_k8s_tail_logs

    v1 = _v1(
        read_namespaced_pod_log=AsyncMock(return_value=""),
    )
    args = TailLogsArgs(pod_name="mypod", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_tail_logs(None, args)

    assert "logs_ok" in result


@pytest.mark.asyncio
async def test_tail_logs_error():
    from workers.k8s_cluster_tools import TailLogsArgs, tool_k8s_tail_logs

    exc = ApiException(status=404, reason="Not Found")
    v1 = _v1(
        read_namespaced_pod_log=AsyncMock(side_effect=exc),
    )
    args = TailLogsArgs(pod_name="absent-pod", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_tail_logs(None, args)

    assert "[DATA] error" in result


# ---------------------------------------------------------------------------
# tool_k8s_check_endpoints (lines 196-217)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_endpoints_ready_and_not_ready():
    from workers.k8s_cluster_tools import CheckEndpointsArgs, tool_k8s_check_endpoints

    addr1 = MagicMock()
    addr2 = MagicMock()
    na = MagicMock()
    subset = MagicMock()
    subset.addresses = [addr1, addr2]
    subset.not_ready_addresses = [na]
    ep = MagicMock()
    ep.subsets = [subset]
    v1 = _v1(read_namespaced_endpoints=AsyncMock(return_value=ep))

    args = CheckEndpointsArgs(service_name="my-svc", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_check_endpoints(None, args)

    assert "endpoints_ok" in result
    assert "ready_addrs=2" in result
    assert "not_ready=1" in result


@pytest.mark.asyncio
async def test_check_endpoints_no_subsets():
    from workers.k8s_cluster_tools import CheckEndpointsArgs, tool_k8s_check_endpoints

    ep = MagicMock()
    ep.subsets = []
    v1 = _v1(read_namespaced_endpoints=AsyncMock(return_value=ep))

    args = CheckEndpointsArgs(service_name="svc", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_check_endpoints(None, args)

    assert "ready_addrs=0" in result
    assert "not_ready=0" in result


@pytest.mark.asyncio
async def test_check_endpoints_api_error():
    from workers.k8s_cluster_tools import CheckEndpointsArgs, tool_k8s_check_endpoints

    exc = ApiException(status=404, reason="Not Found")
    v1 = _v1(read_namespaced_endpoints=AsyncMock(side_effect=exc))
    args = CheckEndpointsArgs(service_name="svc", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_check_endpoints(None, args)

    assert "[DATA] error" in result


# ---------------------------------------------------------------------------
# tool_k8s_patch_resource (lines 238-258)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_resource_success():
    from workers.k8s_cluster_tools import PatchResourceArgs, tool_k8s_patch_resource

    apps = _apps(
        patch_namespaced_deployment=AsyncMock(return_value=None),
    )
    args = PatchResourceArgs(
        name="my-dep",
        namespace="ns",
        patch_json='{"spec":{"replicas":2}}',
    )

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_patch_resource(None, args)

    assert "patch_ok" in result


@pytest.mark.asyncio
async def test_patch_resource_non_object_json():
    from workers.k8s_cluster_tools import PatchResourceArgs, tool_k8s_patch_resource

    apps = _apps()
    args = PatchResourceArgs(
        name="my-dep",
        namespace="ns",
        patch_json="[1,2,3]",
    )

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_patch_resource(None, args)

    assert "patch_json must be a JSON object" in result


@pytest.mark.asyncio
async def test_patch_resource_api_error():
    from workers.k8s_cluster_tools import PatchResourceArgs, tool_k8s_patch_resource

    exc = ApiException(status=403, reason="Forbidden")
    apps = _apps(
        patch_namespaced_deployment=AsyncMock(side_effect=exc),
    )
    args = PatchResourceArgs(
        name="my-dep",
        namespace="ns",
        patch_json='{"spec":{"replicas":2}}',
    )

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_patch_resource(None, args)

    assert "[DATA] error" in result


# ---------------------------------------------------------------------------
# tool_k8s_patch_configmap (lines 284-304)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_configmap_success_with_reasoning():
    from workers.k8s_cluster_tools import PatchConfigMapArgs, tool_k8s_patch_configmap

    v1 = _v1(patch_namespaced_config_map=AsyncMock(return_value=None))
    args = PatchConfigMapArgs(name="cm", namespace="ns", key="k", value="v", reasoning="fix broken config")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_patch_configmap(None, args)

    assert "configmap_patch_ok" in result
    assert "fix broken config" in result


@pytest.mark.asyncio
async def test_patch_configmap_success_no_reasoning():
    from workers.k8s_cluster_tools import PatchConfigMapArgs, tool_k8s_patch_configmap

    v1 = _v1(patch_namespaced_config_map=AsyncMock(return_value=None))
    args = PatchConfigMapArgs(name="cm", namespace="ns", key="k", value="v")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_patch_configmap(None, args)

    assert "configmap_patch_ok" in result


@pytest.mark.asyncio
async def test_patch_configmap_error():
    from workers.k8s_cluster_tools import PatchConfigMapArgs, tool_k8s_patch_configmap

    exc = ApiException(status=404, reason="Not Found")
    v1 = _v1(patch_namespaced_config_map=AsyncMock(side_effect=exc))
    args = PatchConfigMapArgs(name="cm", namespace="ns", key="k", value="v")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_patch_configmap(None, args)

    assert "[DATA] error" in result


# ---------------------------------------------------------------------------
# tool_k8s_apply_rbac_least_privilege (lines 339-443)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rbac_least_privilege_creates_role_and_binding():
    from workers.k8s_cluster_tools import (
        ApplyRbacLeastPrivilegeArgs,
        tool_k8s_apply_rbac_least_privilege,
    )

    rbac = _rbac(
        create_namespaced_role=AsyncMock(return_value=None),
        create_namespaced_role_binding=AsyncMock(return_value=None),
        delete_cluster_role_binding=AsyncMock(return_value=None),
    )
    args = ApplyRbacLeastPrivilegeArgs(
        executor_sa="omni-worker",
        namespace="multi-agent",
        remove_cluster_admin_binding="omni-worker-cluster-admin",
        reasoning="harden rbac",
    )

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.RbacAuthorizationV1Api", return_value=rbac),
        patch("workers.k8s_cluster_tools.client.V1Role", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1ObjectMeta", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1PolicyRule", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1RoleBinding", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1RoleRef", MagicMock()),
        patch("workers.k8s_cluster_tools.client.RbacV1Subject", MagicMock()),
    ):
        result = await tool_k8s_apply_rbac_least_privilege(None, args)

    assert "rbac_hardened" in result
    assert "role_created" in result
    assert "rolebinding_created" in result
    assert "cluster_admin_binding_removed" in result
    assert "harden rbac" in result


@pytest.mark.asyncio
async def test_rbac_least_privilege_replaces_existing_role_and_binding():
    from workers.k8s_cluster_tools import (
        ApplyRbacLeastPrivilegeArgs,
        tool_k8s_apply_rbac_least_privilege,
    )

    conflict = ApiException(status=409, reason="Conflict")
    rbac = _rbac(
        create_namespaced_role=AsyncMock(side_effect=conflict),
        replace_namespaced_role=AsyncMock(return_value=None),
        create_namespaced_role_binding=AsyncMock(side_effect=conflict),
        replace_namespaced_role_binding=AsyncMock(return_value=None),
        delete_cluster_role_binding=AsyncMock(return_value=None),
    )
    args = ApplyRbacLeastPrivilegeArgs(
        executor_sa="omni-worker",
        namespace="multi-agent",
        remove_cluster_admin_binding="omni-worker-cluster-admin",
    )

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.RbacAuthorizationV1Api", return_value=rbac),
        patch("workers.k8s_cluster_tools.client.V1Role", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1ObjectMeta", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1PolicyRule", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1RoleBinding", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1RoleRef", MagicMock()),
        patch("workers.k8s_cluster_tools.client.RbacV1Subject", MagicMock()),
    ):
        result = await tool_k8s_apply_rbac_least_privilege(None, args)

    assert "role_replaced" in result
    assert "rolebinding_replaced" in result


@pytest.mark.asyncio
async def test_rbac_least_privilege_cluster_admin_not_found():
    from workers.k8s_cluster_tools import (
        ApplyRbacLeastPrivilegeArgs,
        tool_k8s_apply_rbac_least_privilege,
    )

    not_found = ApiException(status=404, reason="Not Found")
    rbac = _rbac(
        create_namespaced_role=AsyncMock(return_value=None),
        create_namespaced_role_binding=AsyncMock(return_value=None),
        delete_cluster_role_binding=AsyncMock(side_effect=not_found),
    )
    args = ApplyRbacLeastPrivilegeArgs(
        executor_sa="omni-worker",
        namespace="multi-agent",
        remove_cluster_admin_binding="nonexistent-binding",
    )

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.RbacAuthorizationV1Api", return_value=rbac),
        patch("workers.k8s_cluster_tools.client.V1Role", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1ObjectMeta", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1PolicyRule", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1RoleBinding", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1RoleRef", MagicMock()),
        patch("workers.k8s_cluster_tools.client.RbacV1Subject", MagicMock()),
    ):
        result = await tool_k8s_apply_rbac_least_privilege(None, args)

    assert "cluster_admin_binding_not_found" in result


@pytest.mark.asyncio
async def test_rbac_least_privilege_skip_remove_when_empty():
    from workers.k8s_cluster_tools import (
        ApplyRbacLeastPrivilegeArgs,
        tool_k8s_apply_rbac_least_privilege,
    )

    delete_mock = AsyncMock(return_value=None)
    rbac = _rbac(
        create_namespaced_role=AsyncMock(return_value=None),
        create_namespaced_role_binding=AsyncMock(return_value=None),
        delete_cluster_role_binding=delete_mock,
    )
    args = ApplyRbacLeastPrivilegeArgs(
        executor_sa="omni-worker",
        namespace="multi-agent",
        remove_cluster_admin_binding="",
    )

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.RbacAuthorizationV1Api", return_value=rbac),
        patch("workers.k8s_cluster_tools.client.V1Role", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1ObjectMeta", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1PolicyRule", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1RoleBinding", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1RoleRef", MagicMock()),
        patch("workers.k8s_cluster_tools.client.RbacV1Subject", MagicMock()),
    ):
        result = await tool_k8s_apply_rbac_least_privilege(None, args)

    assert "rbac_hardened" in result
    # remove_cluster_admin_binding is empty string — delete must NOT be called
    delete_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_rbac_least_privilege_fatal_error():
    from workers.k8s_cluster_tools import (
        ApplyRbacLeastPrivilegeArgs,
        tool_k8s_apply_rbac_least_privilege,
    )

    exc = ApiException(status=403, reason="Forbidden")
    rbac = _rbac(
        create_namespaced_role=AsyncMock(side_effect=exc),
    )
    args = ApplyRbacLeastPrivilegeArgs(executor_sa="sa", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.RbacAuthorizationV1Api", return_value=rbac),
        patch("workers.k8s_cluster_tools.client.V1Role", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1ObjectMeta", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1PolicyRule", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1RoleBinding", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1RoleRef", MagicMock()),
        patch("workers.k8s_cluster_tools.client.RbacV1Subject", MagicMock()),
    ):
        result = await tool_k8s_apply_rbac_least_privilege(None, args)

    assert "[DATA] error" in result


# ---------------------------------------------------------------------------
# tool_k8s_rollout_restart (lines 472-499)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollout_restart_success():
    from workers.k8s_cluster_tools import RolloutRestartArgs, tool_k8s_rollout_restart

    apps = _apps(
        patch_namespaced_deployment=AsyncMock(return_value=None),
    )
    args = RolloutRestartArgs(deployment="my-dep", namespace="ns", reasoning="crash loop fix")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_rollout_restart(None, args)

    assert "rollout_restart_ok" in result
    assert "crash loop fix" in result


@pytest.mark.asyncio
async def test_rollout_restart_no_reasoning():
    from workers.k8s_cluster_tools import RolloutRestartArgs, tool_k8s_rollout_restart

    apps = _apps(
        patch_namespaced_deployment=AsyncMock(return_value=None),
    )
    args = RolloutRestartArgs(deployment="dep", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_rollout_restart(None, args)

    assert "rollout_restart_ok" in result


@pytest.mark.asyncio
async def test_rollout_restart_error():
    from workers.k8s_cluster_tools import RolloutRestartArgs, tool_k8s_rollout_restart

    exc = ApiException(status=404, reason="Not Found")
    apps = _apps(
        patch_namespaced_deployment=AsyncMock(side_effect=exc),
    )
    args = RolloutRestartArgs(deployment="dep", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_rollout_restart(None, args)

    assert "[DATA] error" in result


# ---------------------------------------------------------------------------
# tool_k8s_create_or_patch_configmap (lines 532-563)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_or_patch_configmap_created():
    from workers.k8s_cluster_tools import (
        CreateOrPatchConfigMapArgs,
        tool_k8s_create_or_patch_configmap,
    )

    v1 = _v1(
        create_namespaced_config_map=AsyncMock(return_value=None),
    )
    args = CreateOrPatchConfigMapArgs(name="cm", namespace="ns", key="k", value="v", reasoning="fix missing cm")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.V1ConfigMap", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1ObjectMeta", MagicMock()),
    ):
        result = await tool_k8s_create_or_patch_configmap(None, args)

    assert "configmap_created" in result
    assert "fix missing cm" in result


@pytest.mark.asyncio
async def test_create_or_patch_configmap_already_exists_patches():
    from workers.k8s_cluster_tools import (
        CreateOrPatchConfigMapArgs,
        tool_k8s_create_or_patch_configmap,
    )

    conflict = ApiException(status=409, reason="Conflict")
    v1 = _v1(
        create_namespaced_config_map=AsyncMock(side_effect=conflict),
        patch_namespaced_config_map=AsyncMock(return_value=None),
    )
    args = CreateOrPatchConfigMapArgs(name="cm", namespace="ns", key="k", value="newval")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.V1ConfigMap", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1ObjectMeta", MagicMock()),
    ):
        result = await tool_k8s_create_or_patch_configmap(None, args)

    assert "configmap_patched" in result


@pytest.mark.asyncio
async def test_create_or_patch_configmap_other_error_propagates():
    from workers.k8s_cluster_tools import (
        CreateOrPatchConfigMapArgs,
        tool_k8s_create_or_patch_configmap,
    )

    exc = ApiException(status=403, reason="Forbidden")
    v1 = _v1(
        create_namespaced_config_map=AsyncMock(side_effect=exc),
    )
    args = CreateOrPatchConfigMapArgs(name="cm", namespace="ns", key="k", value="v")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.V1ConfigMap", MagicMock()),
        patch("workers.k8s_cluster_tools.client.V1ObjectMeta", MagicMock()),
    ):
        result = await tool_k8s_create_or_patch_configmap(None, args)

    assert "[DATA] error" in result


# ---------------------------------------------------------------------------
# tool_k8s_get_logs (lines 579-599)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_logs_success_with_container():
    from workers.k8s_cluster_tools import GetLogsArgs, tool_k8s_get_logs

    v1 = _v1(
        read_namespaced_pod_log=AsyncMock(return_value="error: something went wrong"),
    )
    args = GetLogsArgs(pod_name="pod", namespace="ns", container="main", lines=100)

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_get_logs(None, args)

    assert "logs_ok" in result
    assert "error: something went wrong" in result
    call_kwargs = v1.read_namespaced_pod_log.call_args
    assert call_kwargs.kwargs.get("container") == "main" or "main" in str(call_kwargs)


@pytest.mark.asyncio
async def test_get_logs_success_no_container():
    from workers.k8s_cluster_tools import GetLogsArgs, tool_k8s_get_logs

    v1 = _v1(
        read_namespaced_pod_log=AsyncMock(return_value="output"),
    )
    args = GetLogsArgs(pod_name="pod", namespace="ns", container="", lines=10)

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_get_logs(None, args)

    assert "logs_ok" in result
    call_kwargs = v1.read_namespaced_pod_log.call_args
    # container kwarg should NOT be present when empty
    assert "container" not in (call_kwargs.kwargs or {})


@pytest.mark.asyncio
async def test_get_logs_error():
    from workers.k8s_cluster_tools import GetLogsArgs, tool_k8s_get_logs

    exc = ApiException(status=404, reason="Not Found")
    v1 = _v1(read_namespaced_pod_log=AsyncMock(side_effect=exc))
    args = GetLogsArgs(pod_name="pod", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_get_logs(None, args)

    assert "[DATA] error" in result


# ---------------------------------------------------------------------------
# tool_k8s_get_events (lines 610-635)
# ---------------------------------------------------------------------------


def _mk_event(typ="Warning", reason="OOMKilled", msg="container killed"):
    e = MagicMock()
    e.type = typ
    e.reason = reason
    e.message = msg
    return e


@pytest.mark.asyncio
async def test_get_events_with_name_filter():
    from workers.k8s_cluster_tools import GetEventsArgs, tool_k8s_get_events

    ev = MagicMock()
    ev.items = [_mk_event()]
    v1 = _v1(list_namespaced_event=AsyncMock(return_value=ev))

    args = GetEventsArgs(namespace="ns", involved_name="my-pod", involved_kind="Pod")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_get_events(None, args)

    assert "events_ok" in result
    assert "OOMKilled" in result
    call_kwargs = v1.list_namespaced_event.call_args
    assert "field_selector" in call_kwargs.kwargs or "my-pod" in str(call_kwargs)


@pytest.mark.asyncio
async def test_get_events_no_name_filter():
    from workers.k8s_cluster_tools import GetEventsArgs, tool_k8s_get_events

    ev = MagicMock()
    ev.items = []
    v1 = _v1(list_namespaced_event=AsyncMock(return_value=ev))

    args = GetEventsArgs(namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_get_events(None, args)

    assert "events_ok" in result
    assert "no events in scope" in result
    call_kwargs = v1.list_namespaced_event.call_args
    assert "field_selector" not in (call_kwargs.kwargs or {})


@pytest.mark.asyncio
async def test_get_events_error():
    from workers.k8s_cluster_tools import GetEventsArgs, tool_k8s_get_events

    exc = ApiException(status=403, reason="Forbidden")
    v1 = _v1(list_namespaced_event=AsyncMock(side_effect=exc))
    args = GetEventsArgs(namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_get_events(None, args)

    assert "[DATA] error" in result


# ---------------------------------------------------------------------------
# tool_k8s_list_resources (lines 648-679)
# ---------------------------------------------------------------------------


def _mk_item(name):
    item = MagicMock()
    item.metadata.name = name
    return item


def _mk_list(*names):
    lst = MagicMock()
    lst.items = [_mk_item(n) for n in names]
    return lst


@pytest.mark.asyncio
async def test_list_resources_pods():
    from workers.k8s_cluster_tools import ListResourcesArgs, tool_k8s_list_resources

    v1 = _v1(list_namespaced_pod=AsyncMock(return_value=_mk_list("pod-a", "pod-b")))
    apps = _apps()

    args = ListResourcesArgs(resource="pods", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_list_resources(None, args)

    assert "list_ok" in result
    assert "pod-a" in result
    assert "n=2" in result


@pytest.mark.asyncio
async def test_list_resources_deployments():
    from workers.k8s_cluster_tools import ListResourcesArgs, tool_k8s_list_resources

    v1 = _v1()
    apps = _apps(
        list_namespaced_deployment=AsyncMock(return_value=_mk_list("dep-a")),
    )

    args = ListResourcesArgs(resource="deployments", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_list_resources(None, args)

    assert "list_ok" in result
    assert "dep-a" in result


@pytest.mark.asyncio
async def test_list_resources_services():
    from workers.k8s_cluster_tools import ListResourcesArgs, tool_k8s_list_resources

    v1 = _v1(list_namespaced_service=AsyncMock(return_value=_mk_list("svc-a")))
    apps = _apps()

    args = ListResourcesArgs(resource="services", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_list_resources(None, args)

    assert "svc-a" in result


@pytest.mark.asyncio
async def test_list_resources_configmaps():
    from workers.k8s_cluster_tools import ListResourcesArgs, tool_k8s_list_resources

    v1 = _v1(list_namespaced_config_map=AsyncMock(return_value=_mk_list("cm-a")))
    apps = _apps()

    args = ListResourcesArgs(resource="configmaps", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_list_resources(None, args)

    assert "cm-a" in result


@pytest.mark.asyncio
async def test_list_resources_secrets():
    from workers.k8s_cluster_tools import ListResourcesArgs, tool_k8s_list_resources

    v1 = _v1(list_namespaced_secret=AsyncMock(return_value=_mk_list("sec-a")))
    apps = _apps()

    args = ListResourcesArgs(resource="secrets", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_list_resources(None, args)

    assert "sec-a" in result


@pytest.mark.asyncio
async def test_list_resources_error():
    from workers.k8s_cluster_tools import ListResourcesArgs, tool_k8s_list_resources

    exc = ApiException(status=403, reason="Forbidden")
    v1 = _v1(list_namespaced_pod=AsyncMock(side_effect=exc))
    apps = _apps()
    args = ListResourcesArgs(resource="pods", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_list_resources(None, args)

    assert "[DATA] error" in result


# ---------------------------------------------------------------------------
# tool_k8s_scale_resource (lines 700-709) — alias
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_resource_delegates():
    from workers.k8s_cluster_tools import ScaleResourceArgs, tool_k8s_scale_resource

    dep = MagicMock()
    dep.spec.replicas = 1
    apps = _apps(
        read_namespaced_deployment=AsyncMock(return_value=dep),
        replace_namespaced_deployment=AsyncMock(return_value=None),
    )
    args = ScaleResourceArgs(name="dep", namespace="ns", replicas=2)

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_scale_resource(None, args)

    assert "scale_ok" in result


# ---------------------------------------------------------------------------
# tool_k8s_delete_pod (lines 728-742)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_pod_success():
    from workers.k8s_cluster_tools import DeletePodArgs, tool_k8s_delete_pod

    v1 = _v1(delete_namespaced_pod=AsyncMock(return_value=None))
    args = DeletePodArgs(name="crash-pod", namespace="ns", reasoning="crash loop")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_delete_pod(None, args)

    assert "delete_pod_ok" in result
    assert "crash loop" in result


@pytest.mark.asyncio
async def test_delete_pod_no_reasoning():
    from workers.k8s_cluster_tools import DeletePodArgs, tool_k8s_delete_pod

    v1 = _v1(delete_namespaced_pod=AsyncMock(return_value=None))
    args = DeletePodArgs(name="pod", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_delete_pod(None, args)

    assert "delete_pod_ok" in result


@pytest.mark.asyncio
async def test_delete_pod_error():
    from workers.k8s_cluster_tools import DeletePodArgs, tool_k8s_delete_pod

    exc = ApiException(status=404, reason="Not Found")
    v1 = _v1(delete_namespaced_pod=AsyncMock(side_effect=exc))
    args = DeletePodArgs(name="pod", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_delete_pod(None, args)

    assert "[DATA] error" in result


# ---------------------------------------------------------------------------
# get_resource_owner (lines 774-808)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_resource_owner_pod_owned_by_deployment_via_rs():
    from workers.k8s_cluster_tools import get_resource_owner

    rs_ref = SimpleNamespace(kind="ReplicaSet", name="my-rs")
    dep_ref = SimpleNamespace(kind="Deployment", name="my-dep")

    pod = MagicMock()
    pod.metadata.owner_references = [rs_ref]

    rs = MagicMock()
    rs.metadata.owner_references = [dep_ref]

    v1 = _v1(read_namespaced_pod=AsyncMock(return_value=pod))
    apps = _apps(
        read_namespaced_replica_set=AsyncMock(return_value=rs),
    )

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await get_resource_owner("my-pod", "ns")

    assert result == ("Deployment", "my-dep")


@pytest.mark.asyncio
async def test_get_resource_owner_pod_owned_by_statefulset():
    from workers.k8s_cluster_tools import get_resource_owner

    sts_ref = SimpleNamespace(kind="StatefulSet", name="my-sts")
    pod = MagicMock()
    pod.metadata.owner_references = [sts_ref]

    v1 = _v1(read_namespaced_pod=AsyncMock(return_value=pod))
    apps = _apps()

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await get_resource_owner("my-pod", "ns")

    assert result == ("StatefulSet", "my-sts")


@pytest.mark.asyncio
async def test_get_resource_owner_standalone_pod():
    from workers.k8s_cluster_tools import get_resource_owner

    pod = MagicMock()
    pod.metadata.owner_references = []

    v1 = _v1(read_namespaced_pod=AsyncMock(return_value=pod))
    apps = _apps()

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await get_resource_owner("standalone-pod", "ns")

    assert result is None


@pytest.mark.asyncio
async def test_get_resource_owner_pod_not_found():
    from workers.k8s_cluster_tools import get_resource_owner

    exc = ApiException(status=404, reason="Not Found")
    v1 = _v1(read_namespaced_pod=AsyncMock(side_effect=exc))
    apps = _apps()

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await get_resource_owner("missing-pod", "ns")

    assert result is None


@pytest.mark.asyncio
async def test_get_resource_owner_rs_not_found():
    from workers.k8s_cluster_tools import get_resource_owner

    rs_ref = SimpleNamespace(kind="ReplicaSet", name="stale-rs")
    pod = MagicMock()
    pod.metadata.owner_references = [rs_ref]

    exc = ApiException(status=404, reason="Not Found")
    v1 = _v1(read_namespaced_pod=AsyncMock(return_value=pod))
    apps = _apps(
        read_namespaced_replica_set=AsyncMock(side_effect=exc),
    )

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await get_resource_owner("pod", "ns")

    assert result is None


@pytest.mark.asyncio
async def test_get_resource_owner_unknown_kind():
    from workers.k8s_cluster_tools import get_resource_owner

    unknown_ref = SimpleNamespace(kind="SomeCustomKind", name="custom-obj")
    pod = MagicMock()
    pod.metadata.owner_references = [unknown_ref]

    v1 = _v1(read_namespaced_pod=AsyncMock(return_value=pod))
    apps = _apps()

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await get_resource_owner("pod", "ns")

    assert result is None


# ---------------------------------------------------------------------------
# tool_k8s_get_deployment_state (lines 818-841)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_deployment_state_healthy():
    from workers.k8s_cluster_tools import GetDeploymentStateArgs, tool_k8s_get_deployment_state

    dep = _dep_mock(replicas=3, ready=3, unavailable=0, observed_gen=5, generation=5)
    apps = _apps(read_namespaced_deployment=AsyncMock(return_value=dep))
    args = GetDeploymentStateArgs(deployment="my-dep", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_get_deployment_state(None, args)

    assert "deployment_state_ok" in result
    assert "healthy=true" in result


@pytest.mark.asyncio
async def test_get_deployment_state_zero_replicas():
    from workers.k8s_cluster_tools import GetDeploymentStateArgs, tool_k8s_get_deployment_state

    dep = _dep_mock(replicas=0, ready=0, unavailable=0, observed_gen=1, generation=1)
    apps = _apps(read_namespaced_deployment=AsyncMock(return_value=dep))
    args = GetDeploymentStateArgs(deployment="dep", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_get_deployment_state(None, args)

    assert "healthy=true" in result


@pytest.mark.asyncio
async def test_get_deployment_state_unhealthy():
    from workers.k8s_cluster_tools import GetDeploymentStateArgs, tool_k8s_get_deployment_state

    dep = _dep_mock(replicas=3, ready=1, unavailable=2, observed_gen=2, generation=3)
    apps = _apps(read_namespaced_deployment=AsyncMock(return_value=dep))
    args = GetDeploymentStateArgs(deployment="dep", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_get_deployment_state(None, args)

    assert "healthy=false" in result


@pytest.mark.asyncio
async def test_get_deployment_state_error():
    from workers.k8s_cluster_tools import GetDeploymentStateArgs, tool_k8s_get_deployment_state

    exc = ApiException(status=404, reason="Not Found")
    apps = _apps(read_namespaced_deployment=AsyncMock(side_effect=exc))
    args = GetDeploymentStateArgs(deployment="dep", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_get_deployment_state(None, args)

    assert "[DATA] error" in result


# ---------------------------------------------------------------------------
# tool_k8s_list_workload_pods (lines 851-880)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_workload_pods_success():
    from workers.k8s_cluster_tools import ListWorkloadPodsArgs, tool_k8s_list_workload_pods

    dep = MagicMock()
    dep.spec.selector.match_labels = {"app": "my-app"}

    pods = MagicMock()
    pod_item = MagicMock()
    pod_item.metadata.name = "my-app-abc123"
    pods.items = [pod_item]

    apps = _apps(read_namespaced_deployment=AsyncMock(return_value=dep))
    v1 = _v1(list_namespaced_pod=AsyncMock(return_value=pods))

    args = ListWorkloadPodsArgs(deployment="my-dep", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_list_workload_pods(None, args)

    assert "workload_pods_ok" in result
    assert "my-app-abc123" in result
    assert "n=1" in result


@pytest.mark.asyncio
async def test_list_workload_pods_empty_selector():
    from workers.k8s_cluster_tools import ListWorkloadPodsArgs, tool_k8s_list_workload_pods

    dep = MagicMock()
    dep.spec.selector.match_labels = {}

    apps = _apps(read_namespaced_deployment=AsyncMock(return_value=dep))
    v1 = _v1()

    args = ListWorkloadPodsArgs(deployment="dep", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_list_workload_pods(None, args)

    assert "empty selector" in result


@pytest.mark.asyncio
async def test_list_workload_pods_error():
    from workers.k8s_cluster_tools import ListWorkloadPodsArgs, tool_k8s_list_workload_pods

    exc = ApiException(status=404, reason="Not Found")
    apps = _apps(read_namespaced_deployment=AsyncMock(side_effect=exc))
    v1 = _v1()

    args = ListWorkloadPodsArgs(deployment="dep", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_list_workload_pods(None, args)

    assert "[DATA] error" in result


# ---------------------------------------------------------------------------
# tool_k8s_get_pod_secret_refs — stale pod fallback (lines 906-922)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pod_secret_refs_stale_pod_resolves_via_label():
    from workers.k8s_cluster_tools import GetPodSecretRefsArgs, tool_k8s_get_pod_secret_refs

    not_found = ApiException(status=404, reason="Not Found")

    env = SimpleNamespace(
        name="DB_PASSWORD",
        value_from=SimpleNamespace(
            secret_key_ref=SimpleNamespace(name="pg-secret", key="password", optional=False),
        ),
    )
    container = SimpleNamespace(name="app", env=[env], env_from=[])
    pod_meta = MagicMock()
    pod_meta.name = "my-app-xyz-latest"
    pod_meta.creation_timestamp = "2026-01-01T00:00:00Z"
    new_pod = SimpleNamespace(spec=SimpleNamespace(containers=[container]), metadata=pod_meta)

    pods_list = MagicMock()
    pods_list.items = [new_pod]

    v1 = _v1(
        read_namespaced_pod=AsyncMock(side_effect=not_found),
        list_namespaced_pod=AsyncMock(return_value=pods_list),
    )
    args = GetPodSecretRefsArgs(pod_name="my-app-xyz-abc-def", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_get_pod_secret_refs(None, args)

    assert "pod_secret_refs_ok" in result
    assert "pg-secret" in result


@pytest.mark.asyncio
async def test_get_pod_secret_refs_stale_pod_no_fallback_pod_name_too_short():
    from workers.k8s_cluster_tools import GetPodSecretRefsArgs, tool_k8s_get_pod_secret_refs

    not_found = ApiException(status=404, reason="Not Found")
    v1 = _v1(read_namespaced_pod=AsyncMock(side_effect=not_found))
    # Pod name with fewer than 3 parts: can't infer deployment
    args = GetPodSecretRefsArgs(pod_name="mypod", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_get_pod_secret_refs(None, args)

    assert "[DATA] error" in result


@pytest.mark.asyncio
async def test_get_pod_secret_refs_container_filter():
    from workers.k8s_cluster_tools import GetPodSecretRefsArgs, tool_k8s_get_pod_secret_refs

    env_main = SimpleNamespace(
        name="SECRET_A",
        value_from=SimpleNamespace(
            secret_key_ref=SimpleNamespace(name="secret-a", key="key-a", optional=False),
        ),
    )
    env_sidecar = SimpleNamespace(
        name="SECRET_B",
        value_from=SimpleNamespace(
            secret_key_ref=SimpleNamespace(name="secret-b", key="key-b", optional=False),
        ),
    )
    container_main = SimpleNamespace(name="main", env=[env_main], env_from=[])
    container_sidecar = SimpleNamespace(name="sidecar", env=[env_sidecar], env_from=[])
    pod = SimpleNamespace(spec=SimpleNamespace(containers=[container_main, container_sidecar]))

    v1 = _v1(read_namespaced_pod=AsyncMock(return_value=pod))
    args = GetPodSecretRefsArgs(pod_name="mypod", namespace="ns", container="main")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_get_pod_secret_refs(None, args)

    assert "secret-a" in result
    assert "secret-b" not in result


@pytest.mark.asyncio
async def test_get_pod_secret_refs_non_404_re_raises():
    from workers.k8s_cluster_tools import GetPodSecretRefsArgs, tool_k8s_get_pod_secret_refs

    exc = ApiException(status=403, reason="Forbidden")
    v1 = _v1(read_namespaced_pod=AsyncMock(side_effect=exc))
    args = GetPodSecretRefsArgs(pod_name="pod", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_get_pod_secret_refs(None, args)

    assert "[DATA] error" in result


# ---------------------------------------------------------------------------
# tool_k8s_get_secret_keys (lines 1000-1001 error path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_secret_keys_error():
    from workers.k8s_cluster_tools import GetSecretKeysArgs, tool_k8s_get_secret_keys

    exc = ApiException(status=404, reason="Not Found")
    v1 = _v1(read_namespaced_secret=AsyncMock(side_effect=exc))
    args = GetSecretKeysArgs(name="missing-secret", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_get_secret_keys(None, args)

    assert "[DATA] error" in result


# ---------------------------------------------------------------------------
# tool_k8s_patch_secret (lines 1030-1057)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_secret_success_with_source():
    from workers.k8s_cluster_tools import PatchSecretArgs, tool_k8s_patch_secret

    v1 = _v1(patch_namespaced_secret=AsyncMock(return_value=None))
    args = PatchSecretArgs(
        name="my-secret",
        namespace="ns",
        key="db-password",
        value="newval",
        value_source="runbook",
        value_source_ref="TICKET-123",
        reasoning="rotating credential",
    )

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_patch_secret(None, args)

    assert "secret_patch_ok" in result
    assert "runbook" in result
    assert "TICKET-123" in result
    assert "rotating credential" in result
    # Value must NOT be in result
    assert "newval" not in result


@pytest.mark.asyncio
async def test_patch_secret_no_source():
    from workers.k8s_cluster_tools import PatchSecretArgs, tool_k8s_patch_secret

    v1 = _v1(patch_namespaced_secret=AsyncMock(return_value=None))
    args = PatchSecretArgs(name="sec", namespace="ns", key="k", value="v")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_patch_secret(None, args)

    assert "secret_patch_ok" in result


@pytest.mark.asyncio
async def test_patch_secret_error():
    from workers.k8s_cluster_tools import PatchSecretArgs, tool_k8s_patch_secret

    exc = ApiException(status=403, reason="Forbidden")
    v1 = _v1(patch_namespaced_secret=AsyncMock(side_effect=exc))
    args = PatchSecretArgs(name="sec", namespace="ns", key="k", value="v")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_patch_secret(None, args)

    assert "[DATA] error" in result


# ---------------------------------------------------------------------------
# tool_k8s_verify_rollout (lines 1069-1097)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_rollout_immediate_success():
    from workers.k8s_cluster_tools import VerifyRolloutArgs, tool_k8s_verify_rollout

    dep = _dep_mock(replicas=2, ready=2, unavailable=0, observed_gen=3, generation=3)
    apps = _apps(read_namespaced_deployment=AsyncMock(return_value=dep))
    args = VerifyRolloutArgs(deployment="dep", namespace="ns", timeout_sec=5, poll_sec=0.5)

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
        patch("workers.k8s_cluster_tools.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await tool_k8s_verify_rollout(None, args)

    assert "rollout_verify_ok" in result


@pytest.mark.asyncio
async def test_verify_rollout_zero_replicas_ok():
    from workers.k8s_cluster_tools import VerifyRolloutArgs, tool_k8s_verify_rollout

    dep = _dep_mock(replicas=0, ready=0, unavailable=0, observed_gen=1, generation=1)
    apps = _apps(read_namespaced_deployment=AsyncMock(return_value=dep))
    args = VerifyRolloutArgs(deployment="dep", namespace="ns", timeout_sec=5, poll_sec=0.5)

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
        patch("workers.k8s_cluster_tools.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await tool_k8s_verify_rollout(None, args)

    assert "rollout_verify_ok" in result


@pytest.mark.asyncio
async def test_verify_rollout_timeout():
    from workers.k8s_cluster_tools import VerifyRolloutArgs, tool_k8s_verify_rollout

    # Always unhealthy: 1 ready out of 3 desired, 2 unavailable
    dep = _dep_mock(replicas=3, ready=1, unavailable=2, observed_gen=1, generation=2)
    apps = _apps(read_namespaced_deployment=AsyncMock(return_value=dep))
    args = VerifyRolloutArgs(deployment="dep", namespace="ns", timeout_sec=5, poll_sec=6.0)

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
        patch("workers.k8s_cluster_tools.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await tool_k8s_verify_rollout(None, args)

    assert "rollout_verify_timeout" in result


@pytest.mark.asyncio
async def test_verify_rollout_error():
    from workers.k8s_cluster_tools import VerifyRolloutArgs, tool_k8s_verify_rollout

    exc = ApiException(status=404, reason="Not Found")
    apps = _apps(read_namespaced_deployment=AsyncMock(side_effect=exc))
    args = VerifyRolloutArgs(deployment="dep", namespace="ns")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_verify_rollout(None, args)

    assert "[DATA] error" in result
