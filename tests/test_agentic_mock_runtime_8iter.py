"""Mock runtime: agentic loop tối đa 8 vòng, không bao giờ omni_mark_resolved — xem output cuối + audit."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from workers.agentic_slow_path import agentic_slow_path_with_llm_and_tools
from workers.settings import WorkerSettings


@pytest.mark.asyncio
async def test_agentic_mock_runtime_full_8_iterations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM mock luôn trả redis_health → 8 vòng → max_iterations (HITL message)."""
    ws = WorkerSettings(
        agentic_slow_path_enabled=True,
        agentic_max_llm_iterations=8,
        agentic_debug_io=False,
    )
    ctx = MagicMock()
    ctx.settings = ws
    audit_calls: list[dict] = []

    async def capture_send(_topic: object, envelope: dict) -> None:
        audit_calls.append(dict(envelope))

    ctx.redis = MagicMock()
    ctx.kafka = MagicMock()
    ctx.kafka.send_dict = AsyncMock(side_effect=capture_send)
    ctx.telegram_chat_id = None
    ctx.semaphore = MagicMock()
    ctx.semaphore.acquire = AsyncMock(return_value="tok")
    ctx.semaphore.release = AsyncMock()
    ctx.ollama_slot_held = False
    ctx.ollama = MagicMock()
    ctx.vector_store = MagicMock()
    ctx.vector_store.upsert = AsyncMock()
    ctx.inbound_user_text = "probe alert"

    chat_calls: list[int] = []

    async def always_redis_health(*_a: object, **kw: object) -> dict:
        chat_calls.append(len(kw.get("messages", [])))
        return {"message": {"content": '{"tool":"redis_health","args":{}}'}}

    ctx.ollama.chat = AsyncMock(side_effect=always_redis_health)
    ctx.ollama.embed = AsyncMock(return_value={"embedding": [0.01] * 768})
    ctx.ollama.keep_alive = "5m"

    async def fake_fetch(_ctx: object, _q: str) -> str:
        return ""

    monkeypatch.setattr(
        "workers.agentic_slow_path.fetch_action_experience_context",
        fake_fetch,
    )

    async def fake_rh(_c: object, _a: dict) -> str:
        return "[DATA] redis ok"

    from workers import tools as tools_mod

    monkeypatch.setitem(tools_mod.TOOL_REGISTRY, "redis_health", fake_rh)

    out = await agentic_slow_path_with_llm_and_tools(
        ctx,
        "Alert: K8sDebugProbe — mock full 8 iter",
        trace="mock-runtime-8iter",
    )

    # --- Assertions (runtime facts) ---
    assert ctx.ollama.chat.await_count == 8, "phải đúng 8 lần gọi LLM"
    assert "Agentic max iterations" in out
    assert ctx.vector_store.upsert.await_count == 0, "không có playbook success → không upsert RAG playbook"

    events = [c.get("event") for c in audit_calls]
    assert events.count("tool_ok") == 8
    assert "max_iterations" in events

    max_ev = next(c for c in audit_calls if c.get("event") == "max_iterations")
    assert max_ev.get("outcome") == "REQUIRES_HUMAN_INTERVENTION"
    tomb = json.loads(max_ev["tombstone"]) if isinstance(max_ev.get("tombstone"), str) else max_ev.get("tombstone")
    traj = json.loads(tomb["trajectory"])
    assert len(traj["scratchpad_tools"]) == 8
    assert all(t.get("tool") == "redis_health" for t in traj["scratchpad_tools"])

    # In ra cho người đọc log pytest -s
    print("\n=== MOCK RUNTIME 8 iter — tóm tắt ===")
    print("final_user_message:", out[:500])
    print("ollama.chat calls:", len(chat_calls), "message_counts_per_call:", chat_calls)
    print("audit events:", events)
    print("RAG vector_store.upsert count:", ctx.vector_store.upsert.await_count)
    print("trajectory tools:", [t.get("tool") for t in traj["scratchpad_tools"]])
