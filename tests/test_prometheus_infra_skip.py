"""Prometheus inbound: skip heavy infra preflight/enrich so agentic prompts stay bounded."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from workers.handlers import _handle_inbound_payload_impl


@pytest.mark.asyncio
async def test_prometheus_skips_infra_preflight_and_enrich(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom_preflight(*_a: object, **_k: object) -> None:
        raise AssertionError("preflight_infra_kb must not run for prometheus")

    async def boom_enrich(*_a: object, **_k: object) -> None:
        raise AssertionError("enrich_working_text_with_infra must not run for prometheus")

    monkeypatch.setattr("workers.handlers.preflight_infra_kb", boom_preflight)
    monkeypatch.setattr("workers.handlers.enrich_working_text_with_infra", boom_enrich)
    monkeypatch.setattr("workers.handlers.try_autonomous_sdk_route", AsyncMock(return_value=None))
    monkeypatch.setattr("workers.handlers.try_fast_path", AsyncMock(return_value=(False, None)))

    captured: dict[str, str] = {}

    async def fake_agentic(_ctx: object, working_text: str, **_kwargs: object) -> str:
        captured["working_text"] = working_text
        return "ok"

    monkeypatch.setattr(
        "workers.agentic_slow_path.agentic_slow_path_with_llm_and_tools",
        fake_agentic,
    )

    ctx = MagicMock()
    ctx.scout_ready = asyncio.Event()
    ctx.scout_ready.set()
    ctx.settings.agentic_slow_path_enabled = True
    ctx.telegram_chat_id = None
    ctx.pod_discovery_pairs = []
    ctx.fallback_inline_commands = None

    payload = {
        "source": "prometheus",
        "trace_id": "t-test-prom-skip",
        "data": {
            "alerts": [
                {
                    "labels": {
                        "alertname": "T",
                        "namespace": "multi-agent",
                        "pod": "p-1",
                    },
                    "annotations": {"summary": "cpu"},
                }
            ]
        },
    }

    out = await _handle_inbound_payload_impl(ctx, payload, "t-test-prom-skip")
    assert out == "ok"
    wt = captured["working_text"]
    assert "[CONTEXT: topology_cache]" not in wt
    assert "[OLLAMA_ANCHOR_EN]" in wt
