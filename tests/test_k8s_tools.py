"""k8s_list_pods — kubernetes_asyncio (mocked)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workers.k8s_tools import (
    _discover_pairs_from_hint,
    resolve_deployment_identity,
    resolve_pod_identity,
    tool_inspect_pod_deep,
    tool_inspect_pod_details,
    tool_k8s_list_pods,
    tool_k8s_rollout_restart,
    tool_list_all_pods_sdk,
    tool_list_namespace_pods,
    tool_resolve_deployment_identity,
    tool_resolve_pod_identity,
)


@pytest.mark.asyncio
async def test_k8s_list_pods_formats_rows() -> None:
    mock_pod = MagicMock()
    mock_pod.metadata.name = "pod-a"
    mock_pod.status.phase = "Running"
    mock_pod.status.pod_ip = "10.1.2.3"
    mock_resp = MagicMock()
    mock_resp.items = [mock_pod]

    with patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock), patch(
        "workers.k8s_tools.client.CoreV1Api"
    ) as m_api:
        inst = MagicMock()
        inst.list_namespaced_pod = AsyncMock(return_value=mock_resp)
        inst.api_client.close = AsyncMock()
        m_api.return_value = inst

        out = await tool_k8s_list_pods(None, {"namespace": "multi-agent"})
        assert "pod-a" in out
        assert "Running" in out
        inst.list_namespaced_pod.assert_awaited_once()
        inst.api_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_k8s_list_pods_without_namespace_uses_list_all() -> None:
    mock_pod = MagicMock()
    mock_pod.metadata.name = "p1"
    mock_pod.metadata.namespace = "ns-a"
    mock_pod.status.phase = "Running"
    mock_pod.status.pod_ip = "10.0.0.1"
    mock_resp = MagicMock()
    mock_resp.items = [mock_pod]

    with patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock), patch(
        "workers.k8s_tools.client.CoreV1Api"
    ) as m_api:
        inst = MagicMock()
        inst.list_pod_for_all_namespaces = AsyncMock(return_value=mock_resp)
        inst.api_client.close = AsyncMock()
        m_api.return_value = inst

        out = await tool_k8s_list_pods(None, {})
        assert "p1" in out
        inst.list_pod_for_all_namespaces.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_namespace_pods_requires_explicit_namespace() -> None:
    out = await tool_list_namespace_pods(None, {})
    assert "namespace" in out.lower()
    assert "k8s_list_pods" in out


@pytest.mark.asyncio
async def test_list_namespace_pods_same_as_alias() -> None:
    mock_pod = MagicMock()
    mock_pod.metadata.name = "x"
    mock_pod.status.phase = "Running"
    mock_pod.status.pod_ip = "1.1.1.1"
    mock_resp = MagicMock()
    mock_resp.items = [mock_pod]

    with patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock), patch(
        "workers.k8s_tools.client.CoreV1Api"
    ) as m_api:
        inst = MagicMock()
        inst.list_namespaced_pod = AsyncMock(return_value=mock_resp)
        inst.api_client.close = AsyncMock()
        m_api.return_value = inst

        a = await tool_list_namespace_pods(None, {"namespace": "ns"})
        b = await tool_k8s_list_pods(None, {"namespace": "ns"})
        assert a == b
        assert "x" in a


def test_discover_pairs_from_hint_exact_prefix_substring() -> None:
    pairs = [
        ("a", "redis-master"),
        ("b", "redis-slave"),
        ("c", "other"),
    ]
    assert _discover_pairs_from_hint("redis-master", pairs) == [("a", "redis-master")]
    r = _discover_pairs_from_hint("redis", pairs)
    assert ("a", "redis-master") in r and ("b", "redis-slave") in r


@pytest.mark.asyncio
async def test_k8s_rollout_restart_explicit_user_executes() -> None:
    ctx = MagicMock()
    ctx.restart_rollout_explicit = True
    ctx.telegram_chat_id = 1
    ctx.redis = MagicMock()

    mock_dep = MagicMock()
    mock_dep.spec.template.metadata.annotations = {}

    with patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock), patch(
        "workers.k8s_tools.client.AppsV1Api"
    ) as m_apps, patch("workers.k8s_tools.execute_rollout_restart", new_callable=AsyncMock) as m_exec:
        m_exec.return_value = "[DATA] ok\n[DIAGNOSIS] done"
        inst = MagicMock()
        inst.read_namespaced_deployment = AsyncMock(return_value=mock_dep)
        inst.list_deployment_for_all_namespaces = AsyncMock()
        inst.api_client.close = AsyncMock()
        m_apps.return_value = inst

        out = await tool_k8s_rollout_restart(ctx, {"deployment": "foo", "namespace": "ns"})
        assert "ok" in out
        m_exec.assert_awaited_once()


@pytest.mark.asyncio
async def test_k8s_rollout_restart_inbound_proactive_skips_confirm() -> None:
    """Proactive path: inbound_proactive True — execute ngay, không [CONFIRM_REQUIRED]."""
    ctx = MagicMock()
    ctx.restart_rollout_explicit = False
    ctx.inbound_proactive = True
    ctx.settings = None
    ctx.telegram_chat_id = 42
    ctx.redis = AsyncMock()

    mock_dep = MagicMock()

    with patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock), patch(
        "workers.k8s_tools.client.AppsV1Api"
    ) as m_apps, patch("workers.k8s_tools.execute_rollout_restart", new_callable=AsyncMock) as m_exec:
        m_exec.return_value = "[DATA] ok\n[DIAGNOSIS] done"
        inst = MagicMock()
        inst.read_namespaced_deployment = AsyncMock(return_value=mock_dep)
        inst.list_deployment_for_all_namespaces = AsyncMock()
        inst.api_client.close = AsyncMock()
        m_apps.return_value = inst

        out = await tool_k8s_rollout_restart(ctx, {"deployment": "foo", "namespace": "ns"})
        assert "ok" in out
        assert "[CONFIRM_REQUIRED]" not in out
        m_exec.assert_awaited_once()
        ctx.redis.setex.assert_not_called()


@pytest.mark.asyncio
async def test_k8s_rollout_restart_sets_pending_when_not_explicit() -> None:
    ctx = MagicMock()
    ctx.restart_rollout_explicit = False
    ctx.settings = None  # tránh MagicMock(settings).lab_unchained truthy → execute_rollout_restart
    ctx.telegram_chat_id = 42
    ctx.redis = AsyncMock()
    ctx.redis.setex = AsyncMock()

    mock_dep = MagicMock()

    with patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock), patch(
        "workers.k8s_tools.client.AppsV1Api"
    ) as m_apps:
        inst = MagicMock()
        inst.read_namespaced_deployment = AsyncMock(return_value=mock_dep)
        inst.list_deployment_for_all_namespaces = AsyncMock()
        inst.api_client.close = AsyncMock()
        m_apps.return_value = inst

        out = await tool_k8s_rollout_restart(ctx, {"deployment": "foo", "namespace": "ns"})
        assert "[CONFIRM_REQUIRED]" in out
        ctx.redis.setex.assert_awaited()


@pytest.mark.asyncio
async def test_inspect_pod_details_resolves_and_metrics() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.k8s_default_namespace = "multi-agent"
    ctx.telegram = None

    ctr = SimpleNamespace(
        name="main",
        resources=SimpleNamespace(
            limits={"cpu": "500m", "memory": "512Mi"},
            requests={},
        ),
    )
    mock_pod_meta = SimpleNamespace(
        metadata=SimpleNamespace(name="redis-abc-xyz"),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[
                SimpleNamespace(restart_count=1, ready=True),
            ],
        ),
        spec=SimpleNamespace(containers=[ctr]),
    )

    list_resp = MagicMock()
    list_resp.items = [mock_pod_meta]

    mbody = {
        "containers": [
            {"usage": {"cpu": "50m", "memory": "128Mi"}},
        ]
    }

    mock_ev = SimpleNamespace(
        reason="BackOff",
        message="backoff",
        type="Warning",
        last_timestamp=None,
        event_time=None,
    )
    ev_resp = MagicMock()
    ev_resp.items = [mock_ev]

    with patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock), patch(
        "workers.k8s_tools.client.CoreV1Api"
    ) as m_core, patch("workers.k8s_tools.CustomObjectsApi") as m_co, patch(
        "workers.k8s_tools.pod_cpu_memory_bar_png_bytes", return_value=b"PNG"
    ):
        v1 = MagicMock()
        mock_scan = MagicMock()
        mock_scan.metadata.name = "redis-abc-xyz"
        mock_scan.metadata.namespace = "multi-agent"
        v1.list_pod_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[mock_scan]))
        v1.read_namespaced_pod = AsyncMock(return_value=mock_pod_meta)
        v1.read_namespaced_pod_log = AsyncMock(return_value="line1\nline2")
        v1.list_namespaced_event = AsyncMock(return_value=ev_resp)
        v1.api_client.close = AsyncMock()
        m_core.return_value = v1

        co = MagicMock()
        co.get_namespaced_custom_object = AsyncMock(return_value=mbody)
        co.api_client.close = AsyncMock()
        m_co.return_value = co

        out = await tool_inspect_pod_deep(ctx, {"pod_name": "redis"})
        assert "redis-abc-xyz" in out
        assert "[DATA]" in out
        assert "[DIAGNOSIS]" in out
        assert "resource" in out.lower()
        co.get_namespaced_custom_object.assert_awaited()
        v1.read_namespaced_pod_log.assert_awaited()
        v1.list_pod_for_all_namespaces.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_pod_identity_cluster_unique() -> None:
    v1 = MagicMock()
    p = MagicMock()
    p.metadata.name = "app-xyz"
    p.metadata.namespace = "prod"
    v1.list_pod_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[p]))
    r = await resolve_pod_identity(v1, "app", None)
    assert r.kind == "resolved"
    assert r.namespace == "prod"
    assert r.pod_name == "app-xyz"


@pytest.mark.asyncio
async def test_resolve_deployment_identity_cluster_unique() -> None:
    apps = MagicMock()
    d = MagicMock()
    d.metadata.name = "omni-gateway"
    d.metadata.namespace = "multi-agent"
    apps.list_deployment_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[d]))
    r = await resolve_deployment_identity(apps, "omni-gateway", None)
    assert r.kind == "resolved"
    assert r.namespace == "multi-agent"
    assert r.deployment_name == "omni-gateway"


@pytest.mark.asyncio
async def test_tool_resolve_pod_identity_resolved() -> None:
    with patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock), patch(
        "workers.k8s_tools.client.CoreV1Api"
    ) as m_api:
        inst = MagicMock()
        p = MagicMock()
        p.metadata.name = "pod-a"
        p.metadata.namespace = "ns1"
        inst.list_pod_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[p]))
        inst.api_client.close = AsyncMock()
        m_api.return_value = inst
        out = await tool_resolve_pod_identity(None, {"pod_name": "pod-a"})
        assert "resolved_pod" in out
        assert "ns1" in out


@pytest.mark.asyncio
async def test_tool_resolve_deployment_identity_resolved() -> None:
    with patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock), patch(
        "workers.k8s_tools.client.AppsV1Api"
    ) as m_api:
        inst = MagicMock()
        d = MagicMock()
        d.metadata.name = "dep-x"
        d.metadata.namespace = "ns1"
        inst.list_deployment_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[d]))
        inst.api_client.close = AsyncMock()
        m_api.return_value = inst
        out = await tool_resolve_deployment_identity(None, {"deployment": "dep-x"})
        assert "resolved_deployment" in out
        assert "dep-x" in out


@pytest.mark.asyncio
async def test_list_all_pods_sdk_smoke() -> None:
    mock_pod = MagicMock()
    mock_pod.metadata.name = "x"
    mock_pod.metadata.namespace = "ns"
    mock_pod.status.phase = "Running"
    mock_pod.status.pod_ip = "-"
    with patch("workers.k8s_tools._load_k8s_config", new_callable=AsyncMock), patch(
        "workers.k8s_tools.client.CoreV1Api"
    ) as m_api:
        inst = MagicMock()
        inst.list_pod_for_all_namespaces = AsyncMock(return_value=MagicMock(items=[mock_pod]))
        inst.api_client.close = AsyncMock()
        m_api.return_value = inst
        out = await tool_list_all_pods_sdk(None, {"limit": 10})
        assert "ns" in out and "x" in out


@pytest.mark.asyncio
async def test_list_all_pods_sdk_god_uses_kubectl_subprocess() -> None:
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.lab_unchained = True
    ctx.inbound_trace_id = "t-god"
    ctx.redis = AsyncMock()
    ctx.redis.xadd = AsyncMock()

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(
        return_value=(
            b"NAMESPACE   NAME    READY   STATUS    RESTARTS   AGE\nns1   pod-a   1/1   Running   0   1d\n",
            b"",
        )
    )

    with patch("workers.k8s_tools.asyncio.create_subprocess_exec", new_callable=AsyncMock) as m_exec:
        m_exec.return_value = mock_proc
        out = await tool_list_all_pods_sdk(ctx, {"limit": 10})
        assert "pod-a" in out
        assert "kubectl" in out.lower()
        m_exec.assert_awaited_once()
        assert m_exec.call_args[0][:4] == ("kubectl", "get", "pods", "-A")
