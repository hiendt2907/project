"""Agentic slow-path: omni_mark_resolved records playbook; no early return on tool OK."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from workers.agentic_slow_path import agentic_slow_path_with_llm_and_tools
from workers.settings import WorkerSettings


@pytest.mark.asyncio
async def test_agentic_resolved_records_playbook(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = WorkerSettings(agentic_slow_path_enabled=True, agentic_max_llm_iterations=4)
    ctx = MagicMock()
    ctx.settings = ws
    ctx.redis = MagicMock()
    ctx.redis.xadd = AsyncMock()
    ctx.telegram_chat_id = None
    ctx.semaphore = MagicMock()
    ctx.semaphore.acquire = AsyncMock(return_value="tok")
    ctx.semaphore.release = AsyncMock()
    ctx.ollama_slot_held = False
    ctx.ollama = MagicMock()
    ctx.vector_store = MagicMock()
    ctx.vector_store.upsert = AsyncMock()
    ctx.inbound_user_text = "check redis"

    calls: list[dict] = []

    async def fake_chat(*_a: object, **_k: object) -> dict:
        n = len(calls)
        calls.append({})
        if n == 0:
            return {"message": {"content": '{"tool":"redis_health","args":{}}'}}
        return {"message": {"content": '{"tool":"omni_mark_resolved","args":{"summary":"ok"}}'}}

    ctx.ollama.chat = AsyncMock(side_effect=fake_chat)
    ctx.ollama.embed = AsyncMock(return_value={"embedding": [0.01] * 768})
    ctx.ollama.keep_alive = "5m"

    async def fake_fetch(_ctx: object, _q: str) -> str:
        return ""

    monkeypatch.setattr(
        "workers.agentic_slow_path.fetch_action_experience_context",
        fake_fetch,
    )

    async def fake_rh(_c: object, _a: dict) -> str:
        return "[DATA] redis up"

    from workers import tools as tools_mod

    monkeypatch.setitem(tools_mod.TOOL_REGISTRY, "redis_health", fake_rh)

    out = await agentic_slow_path_with_llm_and_tools(ctx, "check redis", trace="tr-agentic-1")
    assert "RESOLVED" in out or "ok" in out.lower()
    ctx.vector_store.upsert.assert_awaited()
    kw = ctx.vector_store.upsert.call_args.kwargs
    pay = kw["points"][0].payload
    assert pay.get("routing_source") == "agent_session_resolved"
    assert pay.get("memory_kind") == "playbook"


@pytest.mark.asyncio
async def test_agentic_max_iterations_emits_human_tombstone_with_trajectory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = WorkerSettings(agentic_slow_path_enabled=True, agentic_max_llm_iterations=2)
    ctx = MagicMock()
    ctx.settings = ws
    ctx.redis = MagicMock()
    xadd_calls: list[dict] = []

    async def capture_xadd(_stream: object, fields: dict, **_kw: object) -> bytes:
        xadd_calls.append(dict(fields))
        return b"0-0"

    ctx.redis.xadd = AsyncMock(side_effect=capture_xadd)
    ctx.telegram_chat_id = None
    ctx.semaphore = MagicMock()
    ctx.semaphore.acquire = AsyncMock(return_value="tok")
    ctx.semaphore.release = AsyncMock()
    ctx.ollama_slot_held = False
    ctx.ollama = MagicMock()
    ctx.ollama.chat = AsyncMock(
        return_value={"message": {"content": '{"tool":"redis_health","args":{}}'}}
    )
    ctx.ollama.embed = AsyncMock(return_value={"embedding": [0.01] * 768})
    ctx.ollama.keep_alive = "5m"

    async def fake_fetch(_ctx: object, _q: str) -> str:
        return ""

    monkeypatch.setattr(
        "workers.agentic_slow_path.fetch_action_experience_context",
        fake_fetch,
    )

    async def fake_rh(_c: object, _a: dict) -> str:
        return "[DATA] redis up"

    from workers import tools as tools_mod

    monkeypatch.setitem(tools_mod.TOOL_REGISTRY, "redis_health", fake_rh)

    out = await agentic_slow_path_with_llm_and_tools(ctx, "check redis", trace="tr-max-1")
    assert "Agentic max iterations" in out or "omni_mark_resolved" in out
    max_ev = next(
        (c for c in xadd_calls if c.get("event") == "max_iterations"),
        None,
    )
    assert max_ev is not None
    assert max_ev.get("outcome") == "REQUIRES_HUMAN_INTERVENTION"
    tomb_raw = max_ev.get("tombstone")
    assert tomb_raw

    tomb = json.loads(tomb_raw) if isinstance(tomb_raw, str) else tomb_raw
    assert tomb.get("reason") == "max_iterations"
    assert "trajectory" in tomb
    traj = json.loads(tomb["trajectory"])
    assert "messages" in traj
    assert "scratchpad_tools" in traj
    assert len(traj["scratchpad_tools"]) >= 1


@pytest.mark.asyncio
async def test_agentic_escalate_to_human_audit_and_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = WorkerSettings(agentic_slow_path_enabled=True, agentic_max_llm_iterations=4)
    ws.telegram_admin_chat_id = 999001
    ctx = MagicMock()
    ctx.settings = ws
    ctx.redis = MagicMock()
    xadd_calls: list[dict] = []

    async def capture_xadd(_stream: object, fields: dict, **_kw: object) -> bytes:
        xadd_calls.append(dict(fields))
        return b"0-0"

    ctx.redis.xadd = AsyncMock(side_effect=capture_xadd)
    ctx.telegram_chat_id = None
    ctx.telegram = MagicMock()
    ctx.telegram.send_message = AsyncMock()
    ctx.semaphore = MagicMock()
    ctx.semaphore.acquire = AsyncMock(return_value="tok")
    ctx.semaphore.release = AsyncMock()
    ctx.ollama_slot_held = False
    ctx.ollama = MagicMock()
    ctx.ollama.chat = AsyncMock(
        return_value={
            "message": {
                # Phải là lý do safety/policy — escalate lượt đầu không còn được phép với "thiếu label" thuần
                "content": '{"tool":"escalate_to_human","args":{"reason":"security_policy","detail":"x"}}',
            }
        }
    )
    ctx.ollama.embed = AsyncMock(return_value={"embedding": [0.01] * 768})
    ctx.ollama.keep_alive = "5m"

    async def fake_fetch(_ctx: object, _q: str) -> str:
        return ""

    monkeypatch.setattr(
        "workers.agentic_slow_path.fetch_action_experience_context",
        fake_fetch,
    )

    out = await agentic_slow_path_with_llm_and_tools(
        ctx, "alert text", trace="tr-esc-1", unattended_alert=True
    )
    assert "[REQUIRES_HUMAN]" in out
    assert "security_policy" in out
    esc_ev = next((c for c in xadd_calls if c.get("event") == "escalate_to_human"), None)
    assert esc_ev is not None
    assert esc_ev.get("outcome") == "REQUIRES_HUMAN_INTERVENTION"
    ctx.telegram.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_unattended_premature_escalate_blocked_then_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lượt 1 escalate vì insufficient_data → chặn (business), bắt chạy tool trước."""
    ws = WorkerSettings(agentic_slow_path_enabled=True, agentic_max_llm_iterations=4)
    ctx = MagicMock()
    ctx.settings = ws
    ctx.redis = MagicMock()
    xadd_calls: list[dict] = []

    async def capture_xadd(_stream: object, fields: dict, **_kw: object) -> bytes:
        xadd_calls.append(dict(fields))
        return b"0-0"

    ctx.redis.xadd = AsyncMock(side_effect=capture_xadd)
    ctx.telegram_chat_id = None
    ctx.telegram = None
    ctx.semaphore = MagicMock()
    ctx.semaphore.acquire = AsyncMock(return_value="tok")
    ctx.semaphore.release = AsyncMock()
    ctx.ollama_slot_held = False
    chat_calls: list[int] = []

    async def fake_chat(*_a: object, **_k: object) -> dict:
        i = len(chat_calls)
        chat_calls.append(i)
        if i == 0:
            return {
                "message": {
                    "content": '{"tool":"escalate_to_human","args":{"reason":"insufficient_data","detail":"missing id"}}',
                }
            }
        return {"message": {"content": '{"tool":"list_all_pods_sdk","args":{"limit":20}}'}}

    ctx.ollama = MagicMock()
    ctx.ollama.chat = AsyncMock(side_effect=fake_chat)
    ctx.ollama.embed = AsyncMock(return_value={"embedding": [0.01] * 768})
    ctx.ollama.keep_alive = "5m"

    async def fake_fetch(_ctx: object, _q: str) -> str:
        return ""

    monkeypatch.setattr(
        "workers.agentic_slow_path.fetch_action_experience_context",
        fake_fetch,
    )

    async def fake_list(_c: object, _a: dict) -> str:
        return "[DATA] ok\n[DIAGNOSIS] ns\ta\tRunning\t-\n[STATUS] business_hit"

    from workers import tools as tools_mod

    monkeypatch.setitem(tools_mod.TOOL_REGISTRY, "list_all_pods_sdk", fake_list)

    await agentic_slow_path_with_llm_and_tools(
        ctx, "Alert: FullAudit", trace="tr-prem-esc", unattended_alert=True
    )

    blocked = next((c for c in xadd_calls if c.get("event") == "escalate_blocked_discovery_required"), None)
    assert blocked is not None
    assert blocked.get("business_outcome") == "premature_escalate_blocked"
    assert len(chat_calls) >= 2
