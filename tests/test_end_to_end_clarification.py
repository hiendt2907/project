"""Kịch bản: list pod → discovery; chỉ tên pod → enrich namespace; VM pod có target_type + DEBUG PromQL khi no_data."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import workers.handlers as handlers_mod
from workers.handlers import WorkerHandlerContext, handle_inbound_payload
from workers.settings import WorkerSettings


@pytest.mark.asyncio
async def test_list_then_pod_name_enriches_namespace_and_queries_vm() -> None:
    ws = WorkerSettings()
    storage: dict[str, str] = {}

    async def _get(k: str) -> str | None:
        return storage.get(k)

    async def _set(k: str, v: str, ex: int | None = None) -> None:
        storage[k] = v if isinstance(v, str) else str(v)

    r = AsyncMock()
    r.get = AsyncMock(side_effect=_get)
    r.set = AsyncMock(side_effect=_set)
    ollama = MagicMock()
    ollama.embed = AsyncMock(return_value={"embedding": [0.0] * 768})
    ollama.chat = AsyncMock()
    vector_store = MagicMock()
    vector_store.query_points = AsyncMock(return_value=MagicMock(points=[]))
    ledger = AsyncMock()
    sem = MagicMock()
    sem.acquire = AsyncMock(return_value="t")
    sem.release = AsyncMock()

    list_out = (
        "Pods toàn cluster:\n"
        "multi-agent\tredis-7db59987cd-tsqt4\tRunning\t192.168.1.1\n"
    )
    vm_mock = AsyncMock(
        return_value="[DATA] no_data\n[DIAGNOSIS] test\n[DEBUG] PromQL: sum(rate(container_cpu_usage_seconds_total{namespace=\"multi-agent\",pod=\"redis-7db59987cd-tsqt4\"}[5m]))"
    )

    async def fake_list_all(ctx: WorkerHandlerContext, args: dict) -> str:
        ctx.pod_discovery_pairs = [("multi-agent", "redis-7db59987cd-tsqt4")]
        return list_out

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
                "list_all_pods_sdk": fake_list_all,
                "query_victoria_metrics": vm_mock,
                "query_prometheus_metrics": vm_mock,
            },
            clear=False,
        ):
            await handle_inbound_payload(
                ctx,
                {"text": "kubectl get po -A", "chat_id": 800001, "trace_id": "e1"},
            )
            await handle_inbound_payload(
                ctx,
                {"text": "xem CPU", "chat_id": 800001, "trace_id": "e2"},
            )
            out3 = await handle_inbound_payload(
                ctx,
                {"text": "redis-7db59987cd-tsqt4", "chat_id": 800001, "trace_id": "e3"},
            )

    vm_mock.assert_awaited_once()
    args = vm_mock.call_args[0][1]
    assert args.get("target_type") == "pod"
    assert args.get("namespace") == "multi-agent"
    assert args.get("pod_name") == "redis-7db59987cd-tsqt4"
    assert "[DEBUG] PromQL" in out3
