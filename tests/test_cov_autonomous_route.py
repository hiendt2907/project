"""Coverage tests for src/workers/autonomous_route.py."""
from __future__ import annotations

import os

os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OMNI_OLLAMA_BASE_URL", "http://localhost:11434")
os.environ.setdefault("OMNI_ENV_MODE", "dev")

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _make_ctx(default_ns: str = "multi-agent"):
    return SimpleNamespace(
        settings=SimpleNamespace(k8s_default_namespace=default_ns),
    )


# ---------------------------------------------------------------------------
# Pure helper functions (no I/O)
# ---------------------------------------------------------------------------

def test_ns_from_text_with_flag():
    from workers.autonomous_route import _ns_from_text
    assert _ns_from_text("kubectl get pods -n production", "multi-agent") == "production"


def test_ns_from_text_with_long_flag():
    from workers.autonomous_route import _ns_from_text
    assert _ns_from_text("kubectl get pods --namespace kube-system", "multi-agent") == "kube-system"


def test_ns_from_text_default_fallback():
    from workers.autonomous_route import _ns_from_text
    assert _ns_from_text("check pods", "default") == "default"


def test_ns_from_text_default_fallback_empty_default():
    from workers.autonomous_route import _ns_from_text
    assert _ns_from_text("check pods", "") == "multi-agent"


def test_parse_tail_hint_explicit():
    from workers.autonomous_route import _parse_tail_hint
    assert _parse_tail_hint("tail=50 lines") == 50


def test_parse_tail_hint_full():
    from workers.autonomous_route import _parse_tail_hint
    assert _parse_tail_hint("show full logs") == 200


def test_parse_tail_hint_default():
    from workers.autonomous_route import _parse_tail_hint
    assert _parse_tail_hint("show logs") == 120


def test_parse_tail_hint_clamp_max():
    from workers.autonomous_route import _parse_tail_hint
    assert _parse_tail_hint("tail=999") == 500


def test_parse_tail_hint_clamp_min():
    from workers.autonomous_route import _parse_tail_hint
    assert _parse_tail_hint("tail=0") == 1


def test_parse_kubectl_logs_pod_then_ns():
    from workers.autonomous_route import _parse_kubectl_logs
    pod, ns = _parse_kubectl_logs("kubectl logs omni-worker-abc -n multi-agent")
    assert pod == "omni-worker-abc"
    assert ns == "multi-agent"


def test_parse_kubectl_logs_ns_then_pod():
    from workers.autonomous_route import _parse_kubectl_logs
    pod, ns = _parse_kubectl_logs("kubectl logs -n production my-pod-123")
    assert pod == "my-pod-123"
    assert ns == "production"


def test_parse_kubectl_logs_no_match():
    from workers.autonomous_route import _parse_kubectl_logs
    pod, ns = _parse_kubectl_logs("kubectl logs no-namespace-specified")
    assert pod is None
    assert ns is None


def test_parse_kubectl_logs_with_follow():
    from workers.autonomous_route import _parse_kubectl_logs
    pod, ns = _parse_kubectl_logs("kubectl logs -f my-pod -n staging")
    assert pod == "my-pod"
    assert ns == "staging"


def test_parse_kubectl_describe_pod_then_ns():
    from workers.autonomous_route import _parse_kubectl_describe
    pod, ns = _parse_kubectl_describe("kubectl describe pod my-pod-abc -n multi-agent")
    assert pod == "my-pod-abc"
    assert ns == "multi-agent"


def test_parse_kubectl_describe_ns_then_pod():
    from workers.autonomous_route import _parse_kubectl_describe
    pod, ns = _parse_kubectl_describe("kubectl describe pod -n kube-system coredns-abc")
    assert pod == "coredns-abc"
    assert ns == "kube-system"


def test_parse_kubectl_describe_no_match():
    from workers.autonomous_route import _parse_kubectl_describe
    pod, ns = _parse_kubectl_describe("kubectl describe pod")
    assert pod is None
    assert ns is None


def test_guess_pod_token_found():
    from workers.autonomous_route import _guess_pod_token
    result = _guess_pod_token("xem logs của omni-worker-abc12-xyz99 đi")
    assert result == "omni-worker-abc12-xyz99"


def test_guess_pod_token_not_found():
    from workers.autonomous_route import _guess_pod_token
    result = _guess_pod_token("check cpu usage")
    assert result is None


def test_vietnamese_logs_intent_found():
    from workers.autonomous_route import _vietnamese_logs_intent
    result = _vietnamese_logs_intent("xem log của omni-worker-abc12-xyz99", "multi-agent")
    assert result is not None
    pod, ns, tail = result
    assert pod == "omni-worker-abc12-xyz99"


def test_vietnamese_logs_intent_no_pod():
    from workers.autonomous_route import _vietnamese_logs_intent
    result = _vietnamese_logs_intent("xem log", "multi-agent")
    assert result is None


def test_vietnamese_logs_intent_no_logs():
    from workers.autonomous_route import _vietnamese_logs_intent
    result = _vietnamese_logs_intent("check cpu", "multi-agent")
    assert result is None


def test_vietnamese_logs_intent_tail_keyword():
    from workers.autonomous_route import _vietnamese_logs_intent
    result = _vietnamese_logs_intent("tail logs của omni-worker-abc12-xyz99", "multi-agent")
    assert result is not None


def test_vietnamese_logs_intent_nhat_ky():
    from workers.autonomous_route import _vietnamese_logs_intent
    result = _vietnamese_logs_intent("nhật ký của omni-worker-abc12-xyz99", "multi-agent")
    assert result is not None


# ---------------------------------------------------------------------------
# try_autonomous_sdk_route — main dispatcher
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_too_short_returns_none():
    from workers.autonomous_route import try_autonomous_sdk_route
    ctx = _make_ctx()
    result = await try_autonomous_sdk_route(ctx, "hi")
    assert result is None


@pytest.mark.asyncio
async def test_route_kubectl_top_pods():
    from workers.autonomous_route import try_autonomous_sdk_route
    ctx = _make_ctx()
    with patch("workers.k8s_tools.tool_namespace_pods_top", new=AsyncMock(return_value="pod top output")):
        result = await try_autonomous_sdk_route(ctx, "kubectl top pods -n multi-agent")
    assert result == "pod top output"


@pytest.mark.asyncio
async def test_route_kubectl_get_pods_with_ns():
    from workers.autonomous_route import try_autonomous_sdk_route
    ctx = _make_ctx()
    with patch("workers.k8s_tools.tool_list_namespace_pods", new=AsyncMock(return_value="pods list")):
        result = await try_autonomous_sdk_route(ctx, "kubectl get pods -n staging")
    assert result == "pods list"


@pytest.mark.asyncio
async def test_route_kubectl_get_pods_without_ns_returns_none():
    """kubectl get pods without -n flag does NOT match the route pattern."""
    from workers.autonomous_route import try_autonomous_sdk_route
    ctx = _make_ctx()
    # The pattern requires _RE_NS_FLAG to match too — without -n it falls through
    result = await try_autonomous_sdk_route(ctx, "kubectl get pods")
    # Without NS flag, list_namespace_pods route not triggered
    # But other patterns also don't match, so None
    assert result is None


@pytest.mark.asyncio
async def test_route_kubectl_logs():
    from workers.autonomous_route import try_autonomous_sdk_route
    ctx = _make_ctx()
    with patch("workers.k8s_tools.tool_inspect_pod_deep", new=AsyncMock(return_value="logs output")):
        result = await try_autonomous_sdk_route(ctx, "kubectl logs my-pod -n multi-agent")
    assert result == "logs output"


@pytest.mark.asyncio
async def test_route_kubectl_logs_no_pod_ns():
    """Logs pattern matches but pod/ns parse fails → falls through."""
    from workers.autonomous_route import try_autonomous_sdk_route
    ctx = _make_ctx()
    # No NS flag → _parse_kubectl_logs returns None, None → no dispatch
    result = await try_autonomous_sdk_route(ctx, "kubectl logs")
    assert result is None


@pytest.mark.asyncio
async def test_route_kubectl_describe_pod():
    from workers.autonomous_route import try_autonomous_sdk_route
    ctx = _make_ctx()
    with patch("workers.k8s_tools.tool_inspect_pod_deep", new=AsyncMock(return_value="describe output")):
        result = await try_autonomous_sdk_route(ctx, "kubectl describe pod my-pod-abc -n multi-agent")
    assert result == "describe output"


@pytest.mark.asyncio
async def test_route_kubectl_describe_no_pod_ns():
    from workers.autonomous_route import try_autonomous_sdk_route
    ctx = _make_ctx()
    result = await try_autonomous_sdk_route(ctx, "kubectl describe pod")
    assert result is None


@pytest.mark.asyncio
async def test_route_vietnamese_logs():
    from workers.autonomous_route import try_autonomous_sdk_route
    ctx = _make_ctx()
    with patch("workers.k8s_tools.tool_inspect_pod_deep", new=AsyncMock(return_value="vi logs output")):
        result = await try_autonomous_sdk_route(ctx, "xem log của omni-worker-abc12-xyz99 đi")
    assert result == "vi logs output"


@pytest.mark.asyncio
async def test_route_kubectl_get_nodes():
    from workers.autonomous_route import try_autonomous_sdk_route
    ctx = _make_ctx()
    with patch("workers.k8s_readonly_tools.tool_k8s_list_nodes", new=AsyncMock(return_value="nodes output")):
        result = await try_autonomous_sdk_route(ctx, "kubectl get nodes")
    assert result == "nodes output"


@pytest.mark.asyncio
async def test_route_kubectl_describe_nodes():
    from workers.autonomous_route import try_autonomous_sdk_route
    ctx = _make_ctx()
    with patch("workers.k8s_readonly_tools.tool_k8s_list_nodes", new=AsyncMock(return_value="nodes output")):
        result = await try_autonomous_sdk_route(ctx, "kubectl describe nodes")
    assert result == "nodes output"


@pytest.mark.asyncio
async def test_route_xem_node():
    from workers.autonomous_route import try_autonomous_sdk_route
    ctx = _make_ctx()
    with patch("workers.k8s_readonly_tools.tool_k8s_list_nodes", new=AsyncMock(return_value="nodes list")):
        result = await try_autonomous_sdk_route(ctx, "xem node trong cluster")
    assert result == "nodes list"


@pytest.mark.asyncio
async def test_route_kubectl_get_services():
    from workers.autonomous_route import try_autonomous_sdk_route
    ctx = _make_ctx()
    with patch("workers.k8s_readonly_tools.tool_k8s_list_services", new=AsyncMock(return_value="svc output")):
        result = await try_autonomous_sdk_route(ctx, "kubectl get svc -n multi-agent")
    assert result == "svc output"


@pytest.mark.asyncio
async def test_route_kubectl_get_ingress():
    from workers.autonomous_route import try_autonomous_sdk_route
    ctx = _make_ctx()
    with patch("workers.k8s_readonly_tools.tool_k8s_list_ingress", new=AsyncMock(return_value="ingress output")):
        result = await try_autonomous_sdk_route(ctx, "kubectl get ingress -n production")
    assert result == "ingress output"


@pytest.mark.asyncio
async def test_route_no_match_returns_none():
    from workers.autonomous_route import try_autonomous_sdk_route
    ctx = _make_ctx()
    result = await try_autonomous_sdk_route(ctx, "what is the current deployment strategy")
    assert result is None


@pytest.mark.asyncio
async def test_route_no_settings():
    """ctx without settings falls back to 'multi-agent' default."""
    from workers.autonomous_route import try_autonomous_sdk_route
    ctx = SimpleNamespace()
    result = await try_autonomous_sdk_route(ctx, "what is going on")
    assert result is None
