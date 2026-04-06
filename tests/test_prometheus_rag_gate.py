"""Prometheus inbound: RagGate miss → preflight + enrich (unified với mọi source)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from workers.handlers import _handle_inbound_payload_impl


@pytest.mark.asyncio
async def test_prometheus_runs_preflight_after_gate_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    from pkg.rag.gate import RagGateOutcome

    async def miss_gate(*_a: object, **_k: object) -> RagGateOutcome:
        return RagGateOutcome(hit=False, detail={"reason": "test"})

    calls = {"preflight": 0, "enrich": 0}

    async def ok_preflight(*_a: object, **_k: object) -> object:
        calls["preflight"] += 1
        from workers.infra_preflight import LearnedContext

        return LearnedContext()

    async def ok_enrich(*_a: object, **_k: object) -> str:
        calls["enrich"] += 1
        return "ENRICHED_WORKING"

    monkeypatch.setattr("workers.handlers.evaluate_rag_gate", miss_gate)
    monkeypatch.setattr("workers.handlers.preflight_infra_kb", ok_preflight)
    monkeypatch.setattr("workers.handlers.enrich_working_text_with_infra", ok_enrich)
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
        "trace_id": "t-test-prom-gate",
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

    out = await _handle_inbound_payload_impl(ctx, payload, "t-test-prom-gate")
    assert out == "ok"
    assert calls["preflight"] == 1
    assert calls["enrich"] == 1
    assert captured["working_text"] == "ENRICHED_WORKING"


@pytest.mark.asyncio
async def test_rag_gate_hit_skips_llm_and_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    from pkg.rag.gate import RagGateOutcome

    async def hit_gate(*_a: object, **_k: object) -> RagGateOutcome:
        return RagGateOutcome(
            hit=True,
            formatted="[CONTEXT: k8s_expert] short answer from docs.",
            best_score=0.95,
            collection="k8s_expert",
            detail={},
        )

    async def boom_preflight(*_a: object, **_k: object) -> None:
        raise AssertionError("preflight_infra_kb must not run on RagGate HIT")

    monkeypatch.setattr("workers.handlers.evaluate_rag_gate", hit_gate)
    monkeypatch.setattr("workers.handlers.preflight_infra_kb", boom_preflight)
    monkeypatch.setattr("workers.handlers.try_autonomous_sdk_route", AsyncMock(return_value=None))

    ctx = MagicMock()
    ctx.scout_ready = asyncio.Event()
    ctx.scout_ready.set()
    ctx.settings.agentic_slow_path_enabled = True
    ctx.settings.omni_summary_max_words = 100
    ctx.telegram_chat_id = None
    ctx.pod_discovery_pairs = []
    ctx.fallback_inline_commands = None

    payload = {
        "source": "prometheus",
        "trace_id": "t-hit",
        "data": {
            "alerts": [
                {
                    "labels": {"alertname": "X", "namespace": "ns"},
                    "annotations": {"summary": "cpu high"},
                }
            ]
        },
    }

    out = await _handle_inbound_payload_impl(ctx, payload, "t-hit")
    assert "k8s_expert" in out or "short answer" in out.lower()
