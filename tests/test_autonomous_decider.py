"""Autonomous Decider: cooldown, CLEAR, allowlist."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from fakeredis import FakeAsyncRedis

from workers.autonomous_decider import (
    REDIS_KEY_COOLDOWN_PREFIX,
    _fingerprint,
    _is_clear,
    _sigma_hint,
    _tick,
)
from workers.baseline_snapshot import REDIS_KEY_SNAPSHOT


def test_is_clear() -> None:
    assert _is_clear("CLEAR") is True
    assert _is_clear("  CLEAR\n") is True
    assert _is_clear("```\nCLEAR\n```") is False
    assert _is_clear('{"tool":"x"}') is False


def test_sigma_hint_dr() -> None:
    h = _sigma_hint({"dr": True, "z_cpu": 4.0, "z_mem": 0.5})
    assert "3-Sigma" in h or "Sigma" in h
    assert "4.0" in h or "CPU" in h


def test_fingerprint_stable() -> None:
    m = {"dr": True, "evt": [], "z_cpu": 1.0, "z_mem": None}
    assert _fingerprint(m) == _fingerprint(m)


@pytest.mark.asyncio
async def test_tick_skip_no_trigger() -> None:
    r = FakeAsyncRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": False, "evt": []}))
    ctx = MagicMock()
    ctx.redis = r
    ws = MagicMock()
    await _tick(ctx, ws, "m", 600)
    keys = [k async for k in r.scan_iter(f"{REDIS_KEY_COOLDOWN_PREFIX}*")]
    assert len(keys) == 0


@pytest.mark.asyncio
async def test_tick_clear_sets_cooldown(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    r = FakeAsyncRedis(decode_responses=True)
    manifest = {"dr": True, "z_cpu": 4.0, "z_mem": 0.1, "evt": []}
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps(manifest))
    fp = _fingerprint(manifest)

    ollama = MagicMock()
    ollama.chat = AsyncMock(return_value={"message": {"content": "CLEAR"}})

    ws = MagicMock()
    ws.autonomous_safe_tools = "redis_health,k8s_rollout_restart"
    ws.autonomous_allowed_namespaces = "multi-agent"
    ws.k8s_default_namespace = "multi-agent"
    ws.ollama_keep_alive = "5m"
    ws.autonomous_react_enabled = False

    sem = MagicMock()
    sem.acquire_proactive = AsyncMock(return_value="p0")
    sem.release = AsyncMock()

    ctx = MagicMock()
    ctx.redis = r
    ctx.settings = ws
    ctx.ollama = ollama
    ctx.semaphore = sem
    ctx.telegram = None
    ctx.inbound_proactive = False
    ctx.inbound_trace_id = ""

    await _tick(ctx, ws, "deepseek-r1:8b", 600)

    assert await r.get(REDIS_KEY_COOLDOWN_PREFIX + fp) == "1"
    assert "[AUTONOMOUS_DECIDER_REASON]" in caplog.text
    ollama.chat.assert_called_once()
    sem.acquire_proactive.assert_called_once()
    sem.release.assert_called_once()


@pytest.mark.asyncio
async def test_tick_denies_tool_not_in_allowlist() -> None:
    r = FakeAsyncRedis(decode_responses=True)
    manifest = {"dr": True, "z_cpu": 4.0, "evt": []}
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps(manifest))

    ollama = MagicMock()
    ollama.chat = AsyncMock(
        return_value={"message": {"content": '{"tool":"promql_instant","args":{}}'}}
    )

    ws = MagicMock()
    ws.autonomous_safe_tools = "redis_health"
    ws.autonomous_allowed_namespaces = "multi-agent"
    ws.k8s_default_namespace = "multi-agent"
    ws.ollama_keep_alive = "5m"
    ws.autonomous_react_enabled = False

    sem = MagicMock()
    sem.acquire_proactive = AsyncMock(return_value="p0")
    sem.release = AsyncMock()

    ctx = MagicMock()
    ctx.redis = r
    ctx.settings = ws
    ctx.ollama = ollama
    ctx.semaphore = sem
    ctx.telegram = None
    ctx.inbound_proactive = False
    ctx.inbound_trace_id = ""

    await _tick(ctx, ws, "m", 600)

    keys = [k async for k in r.scan_iter(f"{REDIS_KEY_COOLDOWN_PREFIX}*")]
    assert len(keys) == 0


@pytest.mark.asyncio
async def test_tick_remediation_silent_skips_llm(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    r = FakeAsyncRedis(decode_responses=True)
    manifest = {"dr": True, "z_cpu": 9.0, "remediation_silent": True, "evt": []}
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps(manifest))

    ollama = MagicMock()
    ollama.chat = AsyncMock(return_value={"message": {"content": "CLEAR"}})

    ws = MagicMock()
    ws.autonomous_safe_tools = "redis_health"
    ws.autonomous_allowed_namespaces = "multi-agent"
    ws.k8s_default_namespace = "multi-agent"
    ws.ollama_keep_alive = "5m"
    ws.autonomous_react_enabled = False

    ctx = MagicMock()
    ctx.redis = r
    ctx.settings = ws
    ctx.ollama = ollama
    ctx.semaphore = MagicMock()
    ctx.telegram = None

    await _tick(ctx, ws, "m", 600)

    ollama.chat.assert_not_called()
    assert "[REMEDIATION_SILENT_SKIP]" in caplog.text


@pytest.mark.asyncio
async def test_tick_respects_cooldown() -> None:
    r = FakeAsyncRedis(decode_responses=True)
    manifest = {"dr": True, "z_cpu": 4.0, "evt": []}
    fp = _fingerprint(manifest)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps(manifest))
    await r.set(REDIS_KEY_COOLDOWN_PREFIX + fp, "1", ex=600)

    ollama = MagicMock()
    ollama.chat = AsyncMock(return_value={"message": {"content": "CLEAR"}})

    ws = MagicMock()
    ws.autonomous_safe_tools = "redis_health"
    ws.autonomous_allowed_namespaces = "multi-agent"
    ws.k8s_default_namespace = "multi-agent"
    ws.ollama_keep_alive = "5m"
    ws.autonomous_react_enabled = False

    ctx = MagicMock()
    ctx.redis = r
    ctx.settings = ws
    ctx.ollama = ollama
    ctx.semaphore = MagicMock()
    ctx.telegram = None

    await _tick(ctx, ws, "m", 600)

    ollama.chat.assert_not_called()


@pytest.mark.asyncio
async def test_tick_react_aborted_logs_react_aborted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR)
    r = FakeAsyncRedis(decode_responses=True)
    manifest = {"dr": True, "z_cpu": 4.0, "evt": []}
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps(manifest))

    ollama = MagicMock()
    ollama.chat = AsyncMock(return_value={"message": {"content": ""}})

    ws = MagicMock()
    ws.autonomous_safe_tools = "redis_health"
    ws.autonomous_allowed_namespaces = "multi-agent"
    ws.k8s_default_namespace = "multi-agent"
    ws.ollama_keep_alive = "5m"
    ws.autonomous_react_enabled = True
    ws.react_max_turns = 4
    ws.react_observation_max_chars = 1200
    ws.react_state_redis_ttl_sec = 600
    ws.telegram_admin_chat_id = None

    sem = MagicMock()
    sem.acquire_proactive = AsyncMock(return_value="p0")
    sem.release = AsyncMock()

    ctx = MagicMock()
    ctx.redis = r
    ctx.settings = ws
    ctx.ollama = ollama
    ctx.semaphore = sem
    ctx.telegram = None
    ctx.inbound_proactive = False
    ctx.inbound_trace_id = ""

    await _tick(ctx, ws, "m", 600)

    assert "[REACT_ABORTED]" in caplog.text
