"""Định tuyến SDK trước LLM — kubectl-style → tool (không menu)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from workers.autonomous_route import try_autonomous_sdk_route


@pytest.mark.asyncio
async def test_kubectl_top_routes_to_namespace_pods_top(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_top(_ctx: object, args: dict) -> str:
        return f"TOP_NS={args.get('namespace')}"

    monkeypatch.setattr(
        "workers.k8s_tools.tool_namespace_pods_top",
        fake_top,
    )
    ctx = MagicMock()
    ctx.settings.k8s_default_namespace = "multi-agent"
    out = await try_autonomous_sdk_route(ctx, "kubectl top pod -n multi-agent")
    assert out == "TOP_NS=multi-agent"


@pytest.mark.asyncio
async def test_vietnamese_logs_routes_to_inspect(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_inspect(_ctx: object, args: dict) -> str:
        return f"POD={args.get('pod_name')} tail={args.get('tail_lines')}"

    monkeypatch.setattr(
        "workers.k8s_tools.tool_inspect_pod_deep",
        fake_inspect,
    )
    ctx = MagicMock()
    ctx.settings.k8s_default_namespace = "multi-agent"
    out = await try_autonomous_sdk_route(
        ctx,
        "xem logs con omni-worker-64984fb79f-qrftz -n multi-agent",
    )
    assert "omni-worker-64984fb79f-qrftz" in out
    assert "tail=" in out


@pytest.mark.asyncio
async def test_topology_blurbs_does_not_trigger_vi_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Substring 'log' inside 'topology' must not match logs intent (regression)."""
    called: list[str] = []

    async def fake_inspect(_ctx: object, args: dict) -> str:
        called.append("inspect")
        return "bad"

    monkeypatch.setattr(
        "workers.k8s_tools.tool_inspect_pod_deep",
        fake_inspect,
    )
    ctx = MagicMock()
    ctx.settings.k8s_default_namespace = "multi-agent"
    text = (
        "Alert: X pod=nginx-test-abc123def456 namespace=multi-agent - probe\n"
        "[OLLAMA_ANCHOR_EN]\nHINT: unrelated [CONTEXT] topology blurbs."
    )
    out = await try_autonomous_sdk_route(ctx, text)
    assert out is None
    assert not called
