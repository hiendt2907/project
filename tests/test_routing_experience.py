"""Routing experience: point id, fetch filter, record upsert."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from execution.experience import (
    fetch_action_experience_context,
    record_routing_exhausted_no_data,
    record_routing_from_success,
    routing_experience_point_id,
)
from workers.routing_policy import ROUTING_SOURCE_SLOW_PATH, ROUTING_SOURCE_SLOW_PATH_EXHAUSTED
from workers.slow_path_trace import AttemptRecord
from workers.settings import WorkerSettings


def test_routing_point_id_stable() -> None:
    a = routing_experience_point_id("  Hello  ", "redis_health", {"x": 1})
    b = routing_experience_point_id("hello", "redis_health", {"x": 1})
    assert a == b
    assert routing_experience_point_id("hello", "redis_health", {"x": 2}) != a


@pytest.mark.asyncio
async def test_fetch_action_experience_includes_slow_path_success_routing() -> None:
    """record_routing_from_success stores ROUTING_SOURCE_SLOW_PATH; fetch must return it for retrieval."""
    ws = WorkerSettings()
    ctx = MagicMock()
    ctx.settings = ws
    ctx.semaphore = MagicMock()
    ctx.semaphore.acquire = AsyncMock(return_value="t")
    ctx.semaphore.release = AsyncMock()
    ctx.ollama = MagicMock()
    ctx.ollama.embed = AsyncMock(return_value={"embedding": [0.0] * 768})

    p_route = MagicMock()
    p_route.score = 0.9
    p_route.payload = {
        "routing_source": ROUTING_SOURCE_SLOW_PATH,
        "lesson": "[định tuyến] redis_health — prior match",
    }
    p_sandbox = MagicMock()
    p_sandbox.score = 0.6
    p_sandbox.payload = {"lesson": "sandbox lesson here"}

    ctx.vector_store = MagicMock()
    ctx.vector_store.query_points = AsyncMock(return_value=MagicMock(points=[p_route, p_sandbox]))

    out = await fetch_action_experience_context(ctx, "something long enough")
    assert "sandbox lesson" in out
    assert "redis_health" in out or "định tuyến" in out


@pytest.mark.asyncio
async def test_fetch_action_experience_skips_exhausted_payload() -> None:
    ws = WorkerSettings()
    ctx = MagicMock()
    ctx.settings = ws
    ctx.semaphore = MagicMock()
    ctx.semaphore.acquire = AsyncMock(return_value="t")
    ctx.semaphore.release = AsyncMock()
    ctx.ollama = MagicMock()
    ctx.ollama.embed = AsyncMock(return_value={"embedding": [0.0] * 768})

    p_ex = MagicMock()
    p_ex.score = 0.99
    p_ex.payload = {"routing_source": ROUTING_SOURCE_SLOW_PATH_EXHAUSTED, "lesson": "exhausted noise"}
    p_sb = MagicMock()
    p_sb.score = 0.5
    p_sb.payload = {"lesson": "keep this sandbox"}

    ctx.vector_store = MagicMock()
    ctx.vector_store.query_points = AsyncMock(return_value=MagicMock(points=[p_ex, p_sb]))

    out = await fetch_action_experience_context(ctx, "something long enough")
    assert "keep this sandbox" in out
    assert "exhausted noise" not in out


@pytest.mark.asyncio
async def test_record_routing_exhausted_no_data_upserts() -> None:
    ws = WorkerSettings()
    ctx = MagicMock()
    ctx.settings = ws
    ctx.ollama = MagicMock()
    ctx.ollama.embed = AsyncMock(return_value={"embedding": [0.03] * 768})
    ctx.vector_store = MagicMock()
    ctx.vector_store.upsert = AsyncMock()
    ctx.semaphore = MagicMock()
    ctx.semaphore.acquire = AsyncMock(return_value="tok")
    ctx.semaphore.release = AsyncMock()
    ctx.ollama_slot_held = False

    await record_routing_exhausted_no_data(
        ctx,
        "kiểm tra pod không rõ tên",
        trace_id="t2",
        detail="ValueError('x')",
        attempt_trace=[
            AttemptRecord(
                attempt=1,
                phase="tool_error",
                error_signature="tool_error:missing_pod",
                one_line="Thiếu pod_name",
                detail_full="Thiếu pod_name — ...",
                tool="query_victoria_metrics",
            ),
        ],
        exit_reason="max_attempts",
    )
    ctx.vector_store.upsert.assert_awaited_once()
    pt = ctx.vector_store.upsert.call_args.kwargs["points"][0]
    assert pt.payload["outcome"] == "no_data"
    assert pt.payload["routing_source"] == ROUTING_SOURCE_SLOW_PATH_EXHAUSTED
    assert pt.payload["exit_reason"] == "max_attempts"
    assert pt.payload["tools_attempted"] == ["query_victoria_metrics"]
    assert "tool_error:missing_pod" in pt.payload["error_signatures"]
    assert "attempts_summary" in pt.payload


@pytest.mark.asyncio
async def test_record_routing_from_success_upserts() -> None:
    ws = WorkerSettings()
    ctx = MagicMock()
    ctx.settings = ws
    ctx.inbound_user_text = "kiểm tra redis đi"
    ctx.ollama = MagicMock()
    ctx.ollama.embed = AsyncMock(return_value={"embedding": [0.05] * 768})
    ctx.vector_store = MagicMock()
    ctx.vector_store.upsert = AsyncMock()
    ctx.semaphore = MagicMock()
    ctx.semaphore.acquire = AsyncMock(return_value="tok")
    ctx.semaphore.release = AsyncMock()
    ctx.ollama_slot_held = False

    await record_routing_from_success(
        ctx,
        tool="redis_health",
        args={},
        trace_id="t1",
    )
    ctx.vector_store.upsert.assert_awaited_once()
    call_kw = ctx.vector_store.upsert.call_args.kwargs
    assert call_kw["collection_name"] == "action_experience"
    pt = call_kw["points"][0]
    assert pt.payload["tool"] == "redis_health"
    assert pt.payload["routing_source"] == ROUTING_SOURCE_SLOW_PATH
    assert pt.payload["auto_execute"] is True


@pytest.mark.asyncio
async def test_record_routing_god_auto_execute_shell() -> None:
    ws = WorkerSettings(god_mode=True)
    ctx = MagicMock()
    ctx.settings = ws
    ctx.inbound_user_text = "chạy kubectl get pods cho anh"
    ctx.ollama = MagicMock()
    ctx.ollama.embed = AsyncMock(return_value={"embedding": [0.05] * 768})
    ctx.vector_store = MagicMock()
    ctx.vector_store.upsert = AsyncMock()
    ctx.semaphore = MagicMock()
    ctx.semaphore.acquire = AsyncMock(return_value="tok")
    ctx.semaphore.release = AsyncMock()
    ctx.ollama_slot_held = False

    await record_routing_from_success(
        ctx,
        tool="execute_shell_command",
        args={"command": "kubectl get pods -A"},
        trace_id="tg",
    )
    pt = ctx.vector_store.upsert.call_args.kwargs["points"][0]
    assert pt.payload["auto_execute"] is True
    assert pt.payload["tool"] == "execute_shell_command"


@pytest.mark.asyncio
async def test_record_routing_skips_echo() -> None:
    ctx = MagicMock()
    ctx.settings = WorkerSettings()
    ctx.inbound_user_text = "hi"
    ctx.vector_store = MagicMock()
    ctx.vector_store.upsert = AsyncMock()
    await record_routing_from_success(ctx, tool="echo", args={"msg": "x"}, trace_id="t")
    ctx.vector_store.upsert.assert_not_called()
