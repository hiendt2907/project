"""Slow-path: unknown tool / bad JSON → Ollama conversational fallback (no DLQ from KeyError)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from workers.handlers import (
    WorkerHandlerContext,
    _conversational_fallback,
    _parse_suggestions_json_tail,
)
from workers.settings import WorkerSettings


def test_parse_suggestions_json_tail_strips_machine_line() -> None:
    raw = """Tình trạng: 3 node.

Gợi ý cho Sếp:
list_all_pods_sdk - (quét pod)
redis_expert_check - (Redis)
query_victoria_metrics intent=pod - (VM)

SUGGESTIONS_JSON:["list_all_pods_sdk","redis_expert_check","query_victoria_metrics intent=pod duration=1h"]"""
    head, cmds = _parse_suggestions_json_tail(raw)
    assert "SUGGESTIONS_JSON" not in head
    assert cmds == [
        "list_all_pods_sdk",
        "redis_expert_check",
        "query_victoria_metrics intent=pod duration=1h",
    ]


@pytest.mark.asyncio
async def test_conversational_fallback_calls_ollama_chat() -> None:
    ws = WorkerSettings()
    ollama = AsyncMock()
    ollama.chat = AsyncMock(
        return_value={"message": {"content": "Chào đại ca, em gợi ý hỏi rõ pod/namespace nhé."}}
    )
    ollama.embed = AsyncMock(return_value={"embedding": [0.0] * 768})
    ctx = MagicMock(spec=WorkerHandlerContext)
    ctx.settings = ws
    ctx.ollama = ollama
    ctx.redis = AsyncMock()
    ctx.redis.get = AsyncMock(return_value=None)
    q = MagicMock()
    q.query_points = AsyncMock(return_value=MagicMock(points=[]))
    ctx.vector_store = q

    out = await _conversational_fallback(
        ctx, "kiểm tra hệ thống", "t-1", reason="unknown_tool", detail="ascii"
    )
    assert "đại ca" in out or "pod" in out.lower() or "namespace" in out.lower()
    ollama.chat.assert_awaited_once()
    call_kw = ollama.chat.await_args
    assert call_kw.kwargs["model"] == ws.model_heavy_lifter


@pytest.mark.asyncio
async def test_conversational_fallback_sets_inline_commands_when_json_tail() -> None:
    ws = WorkerSettings(fallback_inline_buttons_enabled=True)
    body = (
        "Tình trạng: ok.\n\nGợi ý cho Sếp:\n"
        "a - (b)\nc - (d)\ne - (f)\n\n"
        'SUGGESTIONS_JSON:["x","y","z"]'
    )
    ollama = AsyncMock()
    ollama.chat = AsyncMock(return_value={"message": {"content": body}})
    ollama.embed = AsyncMock(return_value={"embedding": [0.0] * 768})
    ctx = MagicMock(spec=WorkerHandlerContext)
    ctx.settings = ws
    ctx.ollama = ollama
    ctx.redis = AsyncMock()
    ctx.redis.get = AsyncMock(return_value=None)
    q = MagicMock()
    q.query_points = AsyncMock(return_value=MagicMock(points=[]))
    ctx.vector_store = q

    out = await _conversational_fallback(ctx, "hi", "t-2", reason="x")
    assert "SUGGESTIONS_JSON" not in out
    assert ctx.fallback_inline_commands == ["x", "y", "z"]
