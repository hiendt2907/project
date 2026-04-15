"""
tests/test_k8s_cluster_tools.py

Unit tests for tool_k8s_describe_resource covering ConfigMap, Secret (with value
redaction), and missing-resource error handling.  All K8s SDK calls are mocked so
no cluster connection is required.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workers.k8s_cluster_tools import (
    DescribeResourceArgs,
    GetPodSecretRefsArgs,
    GetSecretKeysArgs,
    tool_k8s_describe_resource,
    tool_k8s_get_pod_secret_refs,
    tool_k8s_get_secret_keys,
)


# ---------------------------------------------------------------------------
# Schema contract — ConfigMap and Secret accepted by DescribeResourceArgs
# ---------------------------------------------------------------------------


def test_schema_accepts_configmap():
    args = DescribeResourceArgs(resource_type="ConfigMap", name="my-cm", namespace="default")
    assert args.resource_type == "ConfigMap"


def test_schema_accepts_secret():
    args = DescribeResourceArgs(resource_type="Secret", name="my-secret", namespace="default")
    assert args.resource_type == "Secret"


def test_schema_accepts_legacy_types():
    for rt in ("Pod", "Deployment", "Service"):
        args = DescribeResourceArgs(resource_type=rt, name="x", namespace="ns")
        assert args.resource_type == rt


def test_schema_rejects_unknown_type():
    import pytest as _pytest
    from pydantic import ValidationError
    with _pytest.raises(ValidationError):
        DescribeResourceArgs(resource_type="Node", name="x", namespace="ns")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_k8s_obj(data: dict) -> MagicMock:
    """Return a mock whose .to_dict() yields *data* and .api_client.close() is async-safe."""
    obj = MagicMock()
    obj.to_dict.return_value = data
    return obj


def _mock_event_list(n: int = 0) -> MagicMock:
    ev = MagicMock()
    ev.items = [MagicMock()] * n
    return ev


def _mock_v1(cm_obj=None, secret_obj=None, ev_n: int = 0, raise_exc=None):
    """Build a CoreV1Api mock wired up for the describe path."""
    v1 = MagicMock()
    v1.api_client = MagicMock()
    v1.api_client.close = AsyncMock()

    if raise_exc is not None:
        v1.read_namespaced_config_map = AsyncMock(side_effect=raise_exc)
        v1.read_namespaced_secret = AsyncMock(side_effect=raise_exc)
    else:
        v1.read_namespaced_config_map = AsyncMock(return_value=cm_obj)
        v1.read_namespaced_secret = AsyncMock(return_value=secret_obj)

    v1.list_namespaced_event = AsyncMock(return_value=_mock_event_list(ev_n))
    return v1


def _mock_apps_v1():
    apps = MagicMock()
    apps.api_client = MagicMock()
    apps.api_client.close = AsyncMock()
    return apps


# ---------------------------------------------------------------------------
# ConfigMap describe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_configmap_success():
    cm_data = {
        "metadata": {"name": "my-cm", "namespace": "default"},
        "data": {"key1": "val1", "key2": "val2"},
    }
    cm_obj = _make_k8s_obj(cm_data)
    v1 = _mock_v1(cm_obj=cm_obj, ev_n=0)
    apps = _mock_apps_v1()

    args = DescribeResourceArgs(resource_type="ConfigMap", name="my-cm", namespace="default")

    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_describe_resource(ctx=None, args=args)

    assert "describe_ok" in result
    assert "kind=ConfigMap" in result
    assert "name=my-cm" in result
    assert "events_n=0" in result
    # ConfigMap data values must be present (no redaction)
    assert "val1" in result
    v1.read_namespaced_config_map.assert_awaited_once_with("my-cm", "default")


@pytest.mark.asyncio
async def test_describe_configmap_returns_data_values():
    """ConfigMap values must NOT be redacted (they are not secrets)."""
    cm_obj = _make_k8s_obj({"data": {"app.conf": "server_name=localhost"}})
    v1 = _mock_v1(cm_obj=cm_obj)
    apps = _mock_apps_v1()

    args = DescribeResourceArgs(resource_type="ConfigMap", name="cfg", namespace="ns")
    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_describe_resource(ctx=None, args=args)

    assert "server_name=localhost" in result


# ---------------------------------------------------------------------------
# Secret describe — redaction contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_secret_redacts_data_values():
    """Secret data values must be replaced with '<redacted>'; keys are preserved."""
    secret_obj = _make_k8s_obj({
        "metadata": {"name": "my-secret", "namespace": "default"},
        "type": "Opaque",
        "data": {"username": "dXNlcg==", "password": "cGFzc3dvcmQ="},
        "string_data": {},
    })
    v1 = _mock_v1(secret_obj=secret_obj, ev_n=0)
    apps = _mock_apps_v1()

    args = DescribeResourceArgs(resource_type="Secret", name="my-secret", namespace="default")
    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_describe_resource(ctx=None, args=args)

    assert "describe_ok" in result
    assert "kind=Secret" in result
    # Keys must appear (for diagnostics)
    assert "username" in result
    assert "password" in result
    # Raw base64 values must NOT appear
    assert "dXNlcg==" not in result
    assert "cGFzc3dvcmQ=" not in result
    # Redaction marker must appear
    assert "<redacted>" in result


@pytest.mark.asyncio
async def test_describe_secret_redacts_string_data_values():
    """string_data values must also be redacted."""
    secret_obj = _make_k8s_obj({
        "data": {},
        "string_data": {"token": "super-secret-token"},
    })
    v1 = _mock_v1(secret_obj=secret_obj)
    apps = _mock_apps_v1()

    args = DescribeResourceArgs(resource_type="Secret", name="s", namespace="ns")
    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_describe_resource(ctx=None, args=args)

    assert "token" in result            # key preserved
    assert "super-secret-token" not in result   # value gone
    assert "<redacted>" in result


@pytest.mark.asyncio
async def test_describe_secret_redacts_metadata_annotations():
    """Secret annotations must be redacted to avoid value leakage via last-applied config."""
    secret_obj = _make_k8s_obj(
        {
            "metadata": {
                "annotations": {
                    "kubectl.kubernetes.io/last-applied-configuration": '{"data":{"password":"c2VjcmV0"}}'
                }
            },
            "data": {"password": "c2VjcmV0"},
            "string_data": {},
        }
    )
    v1 = _mock_v1(secret_obj=secret_obj)
    apps = _mock_apps_v1()
    args = DescribeResourceArgs(resource_type="Secret", name="s", namespace="ns")
    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_describe_resource(ctx=None, args=args)
    assert "last-applied-configuration" in result
    assert "c2VjcmV0" not in result
    assert "<redacted>" in result


@pytest.mark.asyncio
async def test_describe_secret_empty_data_no_error():
    """Secret with no data/string_data must not crash."""
    secret_obj = _make_k8s_obj({"type": "kubernetes.io/service-account-token"})
    v1 = _mock_v1(secret_obj=secret_obj)
    apps = _mock_apps_v1()

    args = DescribeResourceArgs(resource_type="Secret", name="sa-token", namespace="ns")
    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_describe_resource(ctx=None, args=args)

    assert "describe_ok" in result


# ---------------------------------------------------------------------------
# Missing resource — ApiException 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_configmap_not_found():
    from kubernetes_asyncio.client import ApiException

    exc = ApiException(status=404, reason="Not Found")
    v1 = _mock_v1(raise_exc=exc)
    apps = _mock_apps_v1()

    args = DescribeResourceArgs(resource_type="ConfigMap", name="absent-cm", namespace="ns")
    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_describe_resource(ctx=None, args=args)

    assert "404" in result


@pytest.mark.asyncio
async def test_describe_secret_not_found():
    from kubernetes_asyncio.client import ApiException

    exc = ApiException(status=404, reason="Not Found")
    v1 = _mock_v1(raise_exc=exc)
    apps = _mock_apps_v1()

    args = DescribeResourceArgs(resource_type="Secret", name="missing-secret", namespace="ns")
    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
        patch("workers.k8s_cluster_tools.client.AppsV1Api", return_value=apps),
    ):
        result = await tool_k8s_describe_resource(ctx=None, args=args)

    assert "404" in result


@pytest.mark.asyncio
async def test_get_pod_secret_refs_returns_names_and_keys_only():
    sec_ref = SimpleNamespace(name="chaos-pg-secret", key="APP_PASSWORD", optional=False)
    env = SimpleNamespace(
        name="APP_PASSWORD",
        value_from=SimpleNamespace(secret_key_ref=sec_ref),
    )
    env_from = SimpleNamespace(secret_ref=SimpleNamespace(name="shared-secret", optional=True))
    container = SimpleNamespace(name="chaos-victim", env=[env], env_from=[env_from])
    pod = SimpleNamespace(spec=SimpleNamespace(containers=[container]))
    v1 = MagicMock()
    v1.api_client = MagicMock()
    v1.api_client.close = AsyncMock()
    v1.read_namespaced_pod = AsyncMock(return_value=pod)
    args = GetPodSecretRefsArgs(pod_name="chaos-victim-0", namespace="multi-agent")
    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_get_pod_secret_refs(ctx=None, args=args)
    assert "pod_secret_refs_ok" in result
    assert "chaos-pg-secret" in result
    assert "APP_PASSWORD" in result
    assert "shared-secret" in result


@pytest.mark.asyncio
async def test_get_secret_keys_returns_key_catalog_without_values():
    sec = SimpleNamespace(data={"APP_PASSWORD": "c2VjcmV0"}, string_data={"ROTATION_HINT": "dont-show"})
    v1 = MagicMock()
    v1.api_client = MagicMock()
    v1.api_client.close = AsyncMock()
    v1.read_namespaced_secret = AsyncMock(return_value=sec)
    args = GetSecretKeysArgs(name="chaos-pg-secret", namespace="multi-agent")
    with (
        patch("workers.k8s_cluster_tools._load_k8s_config", new_callable=AsyncMock),
        patch("workers.k8s_cluster_tools.client.CoreV1Api", return_value=v1),
    ):
        result = await tool_k8s_get_secret_keys(ctx=None, args=args)
    assert "secret_keys_ok" in result
    assert "APP_PASSWORD" in result
    assert "ROTATION_HINT" in result
    assert "c2VjcmV0" not in result
    assert "dont-show" not in result
