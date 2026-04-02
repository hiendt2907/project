"""Preflight learned map → bypass clarification CPU/RAM mơ hồ."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from workers.clarification import is_ambiguous_resource_check, is_scope_ambiguous_cpu_ram
from workers.handlers import WorkerHandlerContext, handle_inbound_payload
from workers.infra_preflight import LearnedContext, preflight_infra_kb


def test_scope_ambiguous_cpu_detected() -> None:
    assert is_scope_ambiguous_cpu_ram("Kiểm tra CPU, RAM") is True
    assert is_scope_ambiguous_cpu_ram("CPU của host") is False


@pytest.mark.asyncio
async def test_preflight_redis_hit_sets_bypass() -> None:
    r = AsyncMock()

    async def _hget(_key: str, field: str) -> str | None:
        return "multi-agent" if field == "redis" else None

    r.hget = AsyncMock(side_effect=_hget)
    r.get = AsyncMock(return_value=None)
    ctx = MagicMock()
    ctx.redis = r
    ctx.settings = MagicMock()
    ctx.settings.embed_model = "m"
    ctx.settings.ollama_keep_alive = "5m"
    ctx.ollama = MagicMock()
    out = await preflight_infra_kb(ctx, "check cpu redis ram")
    assert out.namespace == "multi-agent"
    assert out.matched_token == "redis"
    assert out.clarification_bypass is True
    r.hget.assert_awaited()


@pytest.mark.asyncio
async def test_ambiguous_bypass_with_learned() -> None:
    learned = LearnedContext(namespace="multi-agent", matched_token="redis", clarification_bypass=True)
    assert is_ambiguous_resource_check("Kiểm tra CPU redis", None, learned=learned) is False


@pytest.mark.asyncio
async def test_handle_ambiguous_still_no_embed_when_no_map(monkeypatch: pytest.MonkeyPatch) -> None:
    from workers import handlers as hmod

    ws = MagicMock()
    ws.embed_model = "m"
    ws.ollama_keep_alive = "5m"
    r = AsyncMock()
    r.hget = AsyncMock(return_value=None)
    r.get = AsyncMock(return_value=None)
    r.set = AsyncMock()
    ollama = MagicMock()
    ollama.embed = AsyncMock()
    ollama.chat = AsyncMock()
    vector_store = MagicMock()
    vector_store.query_points = AsyncMock(return_value=MagicMock(points=[]))
    ledger = AsyncMock()
    sem = MagicMock()
    sem.acquire = AsyncMock(return_value="t")
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
    ctx.scout_ready.set()

    async def _fast_no(*_a: object, **_k: object) -> tuple[bool, str]:
        return False, ""

    monkeypatch.setattr(hmod, "try_fast_path", _fast_no)

    out = await handle_inbound_payload(
        ctx,
        {"text": "Kiểm tra CPU, RAM", "chat_id": 999001, "trace_id": "t-preflight"},
    )
    assert "gom" in out.lower() or "pod" in out.lower()
    ollama.embed.assert_not_called()
