"""Host → psutil → session target=host; follow-up chart dùng VM node/host PromQL (không nhảy sang pod)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import workers.handlers as handlers_mod
from workers.handlers import WorkerHandlerContext, handle_inbound_payload
from workers.settings import WorkerSettings


@pytest.mark.asyncio
async def test_host_to_chart_continuity() -> None:
    ws = WorkerSettings()
    storage: dict[str, str] = {}

    async def _get(k: str) -> str | None:
        return storage.get(k)

    async def _set(k: str, v: str, ex: int | None = None) -> None:
        storage[k] = v if isinstance(v, str) else str(v)

    r = AsyncMock()
    r.get = AsyncMock(side_effect=_get)
    r.set = AsyncMock(side_effect=_set)
    r.delete = AsyncMock()
    ollama = MagicMock()
    ollama.embed = AsyncMock(return_value={"embedding": [0.0] * 768})
    ollama.chat = AsyncMock()
    vector_store = MagicMock()
    vector_store.query_points = AsyncMock(return_value=MagicMock(points=[]))
    ledger = AsyncMock()
    sem = MagicMock()
    sem.acquire = AsyncMock(return_value="t")
    sem.release = AsyncMock()

    vm_mock = AsyncMock(return_value="[DATA] ok\n[DIAGNOSIS] chart host\n[DEBUG] PromQL: sum(rate(node_cpu")

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

    with patch("workers.handlers.extract_entities_llm", AsyncMock(return_value={})):
        with patch.dict(
            handlers_mod.TOOL_REGISTRY,
                {
                    "system_psutil": AsyncMock(return_value="CPU: 12%"),
                    "query_victoria_metrics": vm_mock,
                    "query_prometheus_metrics": vm_mock,
                },
            clear=False,
        ):
            await handle_inbound_payload(
                ctx,
                {"text": "kiểm tra CPU", "chat_id": 900001, "trace_id": "h1"},
            )
            out_h = await handle_inbound_payload(
                ctx,
                {"text": "host", "chat_id": 900001, "trace_id": "h2"},
            )
            assert "CPU:" in out_h
            assert "CONTEXT" in out_h

            out_chart = await handle_inbound_payload(
                ctx,
                {"text": "vẽ chart CPU 1h đi", "chat_id": 900001, "trace_id": "h3"},
            )

    assert "ok" in out_chart.lower() or "chart" in out_chart.lower()
    vm_mock.assert_awaited_once()
    args = vm_mock.call_args[0][1]
    assert args.get("target_type") == "host"
    assert "intent" in args
