"""Handlers: fast-path skip semaphore; slow-path acquire; tool errors retry."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

import workers.handlers as handlers_mod
from workers.handlers import (
    WorkerHandlerContext,
    _slow_path_system_messages_for_ctx,
    resolve_remediation_from_memory,
    slow_path_with_llm_and_tools,
    try_fast_path,
)
from workers.ollama_semaphore import RedisOllamaSemaphore
from workers.settings import WorkerSettings


@pytest.mark.asyncio
async def test_fast_path_no_hit_returns_false() -> None:
    ctx = MagicMock()
    ctx.settings = WorkerSettings()
    ctx.ollama = MagicMock()
    ctx.ollama.embed = AsyncMock(return_value={"embedding": [0.0] * 768})
    ctx.vector_store = MagicMock()
    ctx.vector_store.query_points = AsyncMock(
        side_effect=[
            MagicMock(points=[]),
            MagicMock(points=[]),
        ]
    )

    ok, out = await try_fast_path(ctx, "hello")
    assert ok is False
    assert out is None
    assert ctx.vector_store.query_points.await_count == 2


@pytest.mark.asyncio
async def test_fast_path_routing_experience_after_sop_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = MagicMock()
    ctx.settings = WorkerSettings()
    ctx.ollama = MagicMock()
    ctx.ollama.embed = AsyncMock(return_value={"embedding": [0.1] * 768})

    r_pt = MagicMock()
    r_pt.score = 0.88
    r_pt.payload = {
        "routing_source": "slow_path_success",
        "auto_execute": True,
        "tool": "redis_health",
        "args": {},
    }

    async def fake_tool(_c: object, _a: dict) -> str:
        return "redis_ok"

    from workers import handlers as handlers_mod

    monkeypatch.setitem(handlers_mod.TOOL_REGISTRY, "redis_health", fake_tool)
    ctx.vector_store = MagicMock()
    ctx.vector_store.query_points = AsyncMock(
        side_effect=[
            MagicMock(points=[]),
            MagicMock(points=[r_pt]),
        ]
    )
    ok, out = await try_fast_path(ctx, "check redis health")
    assert ok is True
    assert out == "redis_ok"


@pytest.mark.asyncio
async def test_slow_path_acquires_semaphore_once(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = WorkerSettings()
    r = AsyncMock()
    ollama = MagicMock()
    ollama.chat = AsyncMock(
        side_effect=[
            {"message": {"content": '{"tool":"echo","args":{"msg":"x"}}'}},
        ]
    )
    vector_store = AsyncMock()
    ledger = AsyncMock()
    sem = MagicMock(spec=RedisOllamaSemaphore)
    sem.acquire = AsyncMock(return_value="0")
    sem.release = AsyncMock()

    ctx = WorkerHandlerContext(
        settings=ws,
        redis=r,
        ollama=ollama,
        vector_store=vector_store,
        ledger=ledger,
        semaphore=sem,
        telegram=None,
    )

    out = await slow_path_with_llm_and_tools(ctx, "ping")
    assert out == "x"
    sem.acquire.assert_awaited_once()
    sem.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_slow_path_reply_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = WorkerSettings()
    r = AsyncMock()
    ollama = MagicMock()
    ollama.chat = AsyncMock(
        side_effect=[
            {"message": {"content": '{"tool":"reply","args":{"text":"Xin chào"}}'}},
        ]
    )
    vector_store = AsyncMock()
    ledger = AsyncMock()
    sem = MagicMock(spec=RedisOllamaSemaphore)
    sem.acquire = AsyncMock(return_value="0")
    sem.release = AsyncMock()

    ctx = WorkerHandlerContext(
        settings=ws,
        redis=r,
        ollama=ollama,
        vector_store=vector_store,
        ledger=ledger,
        semaphore=sem,
        telegram=None,
    )

    out = await slow_path_with_llm_and_tools(ctx, "hi")
    assert out == "Xin chào"


@pytest.mark.asyncio
async def test_slow_path_no_data_after_max_json_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = WorkerSettings(slow_path_max_tool_attempts=5)
    r = AsyncMock()
    ollama = MagicMock()
    ollama.chat = AsyncMock(return_value={"message": {"content": "not valid json"}})
    ollama.embed = AsyncMock(return_value={"embedding": [0.01] * 768})
    vector_store = AsyncMock()
    vector_store.upsert = AsyncMock()
    ledger = AsyncMock()
    sem = MagicMock(spec=RedisOllamaSemaphore)
    sem.acquire = AsyncMock(return_value="0")
    sem.release = AsyncMock()

    ctx = WorkerHandlerContext(
        settings=ws,
        redis=r,
        ollama=ollama,
        vector_store=vector_store,
        ledger=ledger,
        semaphore=sem,
        telegram=None,
    )
    ctx.inbound_user_text = "xem cpu pod nào đó"

    monkeypatch.setattr(
        handlers_mod,
        "_repair_json_with_helper",
        AsyncMock(side_effect=ValueError("cannot repair")),
    )
    out = await slow_path_with_llm_and_tools(ctx, ctx.inbound_user_text)

    assert "autopsy_exhausted" in out
    assert "stale_signature" in out
    assert "parse_json" in out
    assert ollama.chat.await_count == 3
    vector_store.upsert.assert_awaited()


@pytest.mark.asyncio
async def test_slow_path_five_chats_when_stale_streak_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = WorkerSettings(slow_path_max_tool_attempts=5, slow_path_stale_signature_streak=8)
    r = AsyncMock()
    ollama = MagicMock()
    ollama.chat = AsyncMock(return_value={"message": {"content": "not valid json"}})
    ollama.embed = AsyncMock(return_value={"embedding": [0.01] * 768})
    vector_store = AsyncMock()
    vector_store.upsert = AsyncMock()
    ledger = AsyncMock()
    sem = MagicMock(spec=RedisOllamaSemaphore)
    sem.acquire = AsyncMock(return_value="0")
    sem.release = AsyncMock()

    ctx = WorkerHandlerContext(
        settings=ws,
        redis=r,
        ollama=ollama,
        vector_store=vector_store,
        ledger=ledger,
        semaphore=sem,
        telegram=None,
    )
    ctx.inbound_user_text = "xem cpu pod nào đó"

    monkeypatch.setattr(
        handlers_mod,
        "_repair_json_with_helper",
        AsyncMock(side_effect=ValueError("cannot repair")),
    )
    out = await slow_path_with_llm_and_tools(ctx, ctx.inbound_user_text)

    assert "max_attempts" in out
    assert ollama.chat.await_count == 5


@pytest.mark.asyncio
async def test_resolve_remediation_logs_sop_score(caplog: pytest.LogCaptureFixture) -> None:
    ctx = MagicMock(spec=WorkerHandlerContext)
    ctx.settings = WorkerSettings()
    ctx.ollama = MagicMock()
    ctx.ollama.embed = AsyncMock(return_value={"embedding": [0.1] * 768})
    pt = MagicMock()
    pt.score = 0.91
    pt.payload = {"auto_execute": True, "tool": "reply", "args": {"msg": "ok"}}
    ctx.vector_store = MagicMock()
    ctx.vector_store.query_points = AsyncMock(return_value=MagicMock(points=[pt]))

    with caplog.at_level(logging.INFO, logger="workers.handlers"):
        ok, out, tool = await resolve_remediation_from_memory(
            ctx, "canonical text", trace="t-score", score_threshold=0.85
        )

    assert ok is True
    assert tool == "reply"
    assert "remediation_sop_hit score=0.9100" in caplog.text
    assert "bypassing LLM" in caplog.text


@pytest.mark.asyncio
async def test_fast_path_sop_emits_log_react_json_v3_fast_path_hit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fast path SOP hit logs one-line JSON with reasoning_path v3_fast_path_hit (Loki)."""
    import logging

    ctx = MagicMock(spec=WorkerHandlerContext)
    ctx.settings = WorkerSettings()
    ctx.ollama = MagicMock()
    ctx.ollama.embed = AsyncMock(return_value={"embedding": [0.1] * 768})
    pt = MagicMock()
    pt.score = 0.91
    pt.payload = {"auto_execute": True, "tool": "reply", "args": {"text": "ok"}}
    ctx.vector_store = MagicMock()
    ctx.vector_store.query_points = AsyncMock(return_value=MagicMock(points=[pt]))

    with caplog.at_level(logging.INFO):
        ok, out = await try_fast_path(ctx, "user text", trace="trace-fp")
    assert ok is True
    assert out == "ok"
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "v3_fast_path_hit" in joined
    assert "trace-fp" in joined


@pytest.mark.asyncio
async def test_fast_path_sop_skips_ollama_chat() -> None:
    """Fast path SOP: embed + RAG + tool — không gọi ollama.chat (kiểm toán §4)."""
    ctx = MagicMock(spec=WorkerHandlerContext)
    ctx.settings = WorkerSettings()
    ctx.ollama = MagicMock()
    ctx.ollama.embed = AsyncMock(return_value={"embedding": [0.1] * 768})
    ctx.ollama.chat = AsyncMock()
    pt = MagicMock()
    pt.score = 0.91
    pt.payload = {"auto_execute": True, "tool": "reply", "args": {"text": "ok"}}
    ctx.vector_store = MagicMock()
    ctx.vector_store.query_points = AsyncMock(return_value=MagicMock(points=[pt]))

    ok, out = await try_fast_path(ctx, "user text", trace="t-skip-chat")
    assert ok is True
    assert out == "ok"
    ctx.ollama.embed.assert_awaited()
    ctx.ollama.chat.assert_not_awaited()


def test_slow_path_system_messages_god_vs_sdk() -> None:
    ctx = MagicMock(spec=WorkerHandlerContext)
    ctx.settings = WorkerSettings()
    msgs = _slow_path_system_messages_for_ctx(ctx)
    assert len(msgs) == 2
    assert "SRE Command Generator" in msgs[0]["content"]
    assert "SDK-only" in msgs[1]["content"] or "Cấm** subprocess" in msgs[1]["content"]

    ctx.settings = WorkerSettings(god_mode=True)
    msgs_g = _slow_path_system_messages_for_ctx(ctx)
    assert "SRE Command Generator" in msgs_g[0]["content"]
    assert "God mode" in msgs_g[1]["content"] or "god" in msgs_g[1]["content"].lower()
    assert "execute_shell_command" in msgs_g[1]["content"]
    assert "kubectl top pods -A" in msgs_g[1]["content"]
