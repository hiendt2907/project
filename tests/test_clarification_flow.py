"""Clarification + session_state: ambiguous CPU/RAM → gom slot; Redis; tool khi đủ pod."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workers.clarification import (
    is_ambiguous_resource_check,
    merge_clarification_context,
    parse_namespace_hint,
    parse_resource_followup,
)
from workers.handlers import WorkerHandlerContext, handle_inbound_payload
from workers.session_state import (
    PENDING_AWAIT_VM_SLOTS,
    SessionState,
    redis_key_session,
)
from workers.settings import WorkerSettings
import workers.handlers as handlers_mod


def test_ambiguous_cpu_ram_no_target() -> None:
    assert is_ambiguous_resource_check("Kiểm tra CPU, RAM") is True
    assert is_ambiguous_resource_check("check cpu") is True


def test_not_ambiguous_when_host_named() -> None:
    assert is_ambiguous_resource_check("Kiểm tra CPU trên host") is False


def test_not_ambiguous_when_pod_named() -> None:
    assert is_ambiguous_resource_check("CPU của pod omni-worker") is False


def test_not_ambiguous_when_namespace() -> None:
    assert is_ambiguous_resource_check("RAM namespace multi-agent") is False


def test_merged_context_not_ambiguous() -> None:
    m = merge_clarification_context(
        original_user_text="check cpu",
        followup_text="Host",
        target="host",
        detail=None,
    )
    assert is_ambiguous_resource_check(m) is False


def test_parse_followup_host_and_pod() -> None:
    assert parse_resource_followup("1") == ("host", None)
    assert parse_resource_followup("Của Host") == ("host", None)
    assert parse_resource_followup("pod omni-worker") == ("pod", "omni-worker")
    assert parse_resource_followup("1 pod cụ thể đi") == ("pod", None)
    assert parse_resource_followup("một pod") == ("pod", None)
    assert parse_resource_followup("pod cụ thể") == ("pod", None)


def test_parse_namespace_hint() -> None:
    assert parse_namespace_hint("ở namespace multi-agent") == "multi-agent"
    assert parse_namespace_hint("namespace multi-agent") == "multi-agent"


@pytest.mark.asyncio
async def test_handle_ambiguous_returns_question_no_llm() -> None:
    ws = WorkerSettings()
    r = AsyncMock()
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
    out = await handle_inbound_payload(
        ctx,
        {"text": "Kiểm tra CPU, RAM", "chat_id": 999001, "trace_id": "t-clarify"},
    )
    assert "gom" in out.lower() or "pod" in out.lower()
    ollama.embed.assert_not_called()
    ollama.chat.assert_not_called()
    r.set.assert_awaited()
    call_kw = r.set.await_args
    assert call_kw[0][0] == redis_key_session(999001)


@pytest.mark.asyncio
async def test_handle_followup_after_wait_merges_and_calls_slow_path() -> None:
    ws = WorkerSettings()
    r = AsyncMock()
    stored = SessionState(
        last_goal="Kiểm tra CPU",
        pending_action=PENDING_AWAIT_VM_SLOTS,
        accumulated_vm_slots={"intent": "cpu"},
        turn_count=1,
        recent_messages=[
            {"role": "user", "content": "Kiểm tra CPU"},
            {"role": "assistant", "content": "nudge"},
        ],
    ).model_dump_json()
    r.get = AsyncMock(return_value=stored)
    r.set = AsyncMock()
    ollama = MagicMock()
    ollama.embed = AsyncMock(return_value={"embedding": [0.0] * 768})
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
    with patch("workers.handlers.extract_entities_llm", AsyncMock(return_value={})):
        with patch.dict(
            handlers_mod.TOOL_REGISTRY,
            {"system_psutil": AsyncMock(return_value="psutil_ok")},
            clear=False,
        ):
            out = await handle_inbound_payload(
                ctx,
                {"text": "Host", "chat_id": 999002, "trace_id": "t-follow"},
            )
    assert "psutil_ok" in out
    assert "[CONTEXT:" in out
    ollama.embed.assert_not_called()
    ollama.chat.assert_not_called()
    r.set.assert_awaited()


@pytest.mark.asyncio
async def test_context_accumulation_three_messages_then_vm_tool() -> None:
    """User rải intent → namespace → workload qua 3 tin; đủ pod mới gọi query_victoria_metrics."""
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

    vm_mock = AsyncMock(return_value="[DATA] ok\n[DIAGNOSIS] xong")

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
            {"query_victoria_metrics": vm_mock, "query_prometheus_metrics": vm_mock},
            clear=False,
        ):
            out1 = await handle_inbound_payload(
                ctx,
                {"text": "xem RAM", "chat_id": 777001, "trace_id": "t-acc-1"},
            )
            assert "pod" in out1.lower() or "workload" in out1.lower()
            vm_mock.assert_not_called()

            await handle_inbound_payload(
                ctx,
                {"text": "namespace monitoring", "chat_id": 777001, "trace_id": "t-acc-2"},
            )
            vm_mock.assert_not_called()

            out3 = await handle_inbound_payload(
                ctx,
                {"text": "redis", "chat_id": 777001, "trace_id": "t-acc-3"},
            )

    assert "ok" in out3.lower() or "xong" in out3.lower()
    vm_mock.assert_awaited_once()
    passed_args = vm_mock.call_args[0][1]
    assert passed_args["intent"] == "ram"
    assert passed_args["namespace"] == "monitoring"
    assert passed_args["pod_name"] == "redis"
    assert passed_args.get("target_type") == "pod"
