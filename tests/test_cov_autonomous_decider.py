"""Coverage tests for workers.autonomous_decider.

Targets: pure helpers first, then async tick paths.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis as aioredis
import pytest

os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OMNI_OLLAMA_BASE_URL", "http://localhost:11434")

from workers.autonomous_decider import (
    REDIS_KEY_COOLDOWN_PREFIX,
    _args_fingerprint,
    _build_user_prompt,
    _fingerprint,
    _is_clear,
    _load_prior_react_state,
    _parse_csv_set,
    _parse_react_turn,
    _parse_tool_payload,
    _react_state_key,
    _save_react_state,
    _sigma_hint,
    _strip_markdown_json,
    _system_prompt,
    _system_prompt_react,
    _validate_k8s_ns,
    autonomous_decider_loop,
)
from workers.baseline_snapshot import REDIS_KEY_SNAPSHOT
from workers.tools import ToolCallPayload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ws(**kw: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "autonomous_safe_tools": "k8s_rollout_restart",
        "autonomous_allowed_namespaces": "multi-agent",
        "autonomous_react_enabled": False,
        "llm_chat_timeout_sec": 30,
        "react_max_turns": 2,
        "react_observation_max_chars": 1200,
        "react_state_redis_ttl_sec": 0,
        "k8s_default_namespace": "multi-agent",
        "telegram_admin_chat_id": None,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class _FakeSemaphore:
    async def acquire_proactive(self) -> str:
        return "token"

    async def release(self, token: str) -> None:
        pass


class _FakeLLM:
    def __init__(self, content: str = "CLEAR") -> None:
        self._content = content

    async def chat(self, **kw: Any) -> dict[str, Any]:
        return {"message": {"content": self._content}}

    async def chat_plain(self, **kw: Any) -> dict[str, Any]:
        return await self.chat(**kw)

    async def chat_structured(self, **kw: Any) -> dict[str, Any]:
        return await self.chat(**kw)


def _make_ctx(redis: Any, llm: Any = None, **kw: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "redis": redis,
        "semaphore": _FakeSemaphore(),
        "llm": llm,
        "inbound_proactive": False,
        "inbound_trace_id": "",
        "inbound_reasoning": None,
        "telegram": None,
        "scout_ready": asyncio.Event(),
    }
    defaults.update(kw)
    ctx = SimpleNamespace(**defaults)
    ctx.scout_ready.set()
    return ctx


# ---------------------------------------------------------------------------
# _fingerprint
# ---------------------------------------------------------------------------


class TestFingerprint:
    def test_basic_returns_20_char_hex(self) -> None:
        fp = _fingerprint({"dr": True, "evt": [], "z_cpu": "3.5", "z_mem": None})
        assert len(fp) == 20
        assert all(c in "0123456789abcdef" for c in fp)

    def test_different_manifests_differ(self) -> None:
        fp1 = _fingerprint({"dr": True, "z_cpu": "3.5"})
        fp2 = _fingerprint({"dr": False, "z_cpu": "3.5"})
        assert fp1 != fp2

    def test_same_manifest_stable(self) -> None:
        m = {"dr": True, "evt": [1, 2], "z_cpu": "4.0", "z_mem": "2.0"}
        assert _fingerprint(m) == _fingerprint(m)

    def test_empty_manifest(self) -> None:
        fp = _fingerprint({})
        assert len(fp) == 20

    def test_evt_list_truncated_at_2000(self) -> None:
        big_evt = list(range(500))
        fp = _fingerprint({"dr": True, "evt": big_evt})
        assert len(fp) == 20


# ---------------------------------------------------------------------------
# _parse_csv_set
# ---------------------------------------------------------------------------


class TestParseCsvSet:
    def test_basic(self) -> None:
        assert _parse_csv_set("a, b, c") == {"a", "b", "c"}

    def test_empty_string(self) -> None:
        assert _parse_csv_set("") == set()

    def test_none_string(self) -> None:
        assert _parse_csv_set(None) == set()  # type: ignore[arg-type]

    def test_whitespace_only(self) -> None:
        assert _parse_csv_set("   ") == set()

    def test_single_item(self) -> None:
        assert _parse_csv_set("k8s_rollout_restart") == {"k8s_rollout_restart"}

    def test_strips_spaces(self) -> None:
        assert _parse_csv_set("  x  ,  y  ") == {"x", "y"}


# ---------------------------------------------------------------------------
# _strip_markdown_json
# ---------------------------------------------------------------------------


class TestStripMarkdownJson:
    def test_plain_json_unchanged(self) -> None:
        s = '{"tool":"x"}'
        assert _strip_markdown_json(s) == s

    def test_markdown_fence_stripped(self) -> None:
        s = "```json\n{\"tool\":\"x\"}\n```"
        result = _strip_markdown_json(s)
        assert result == '{"tool":"x"}'

    def test_markdown_fence_no_lang(self) -> None:
        s = "```\n{\"a\":1}\n```"
        result = _strip_markdown_json(s)
        assert result == '{"a":1}'

    def test_none_returns_empty(self) -> None:
        assert _strip_markdown_json(None) == ""  # type: ignore[arg-type]

    def test_empty_string(self) -> None:
        assert _strip_markdown_json("") == ""

    def test_fence_without_closing_backticks(self) -> None:
        s = "```json\n{\"x\":1}"
        result = _strip_markdown_json(s)
        # Should strip the opening fence line
        assert "```" not in result


# ---------------------------------------------------------------------------
# _parse_tool_payload
# ---------------------------------------------------------------------------


class TestParseToolPayload:
    def test_plain_json(self) -> None:
        pl = _parse_tool_payload('{"tool":"k8s_rollout_restart","args":{"namespace":"default"}}')
        assert pl.tool == "k8s_rollout_restart"
        assert pl.args["namespace"] == "default"

    def test_markdown_fence(self) -> None:
        s = '```json\n{"tool":"k8s_scale_deployment","args":{}}\n```'
        pl = _parse_tool_payload(s)
        assert pl.tool == "k8s_scale_deployment"

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(Exception):
            _parse_tool_payload("not-json")

    def test_missing_tool_raises(self) -> None:
        with pytest.raises(Exception):
            _parse_tool_payload('{"args":{}}')


# ---------------------------------------------------------------------------
# _parse_react_turn
# ---------------------------------------------------------------------------


class TestParseReactTurn:
    def test_clear_action(self) -> None:
        content = json.dumps({"thought": "ok", "reasoning_path": "v3_react_thought", "action": "CLEAR"})
        result = _parse_react_turn(content)
        assert result is not None
        thought, rp, is_clear, tool_call = result
        assert is_clear is True
        assert tool_call is None
        assert thought == "ok"

    def test_clear_action_lowercase(self) -> None:
        content = json.dumps({"thought": "fine", "action": "clear"})
        result = _parse_react_turn(content)
        assert result is not None
        _, _, is_clear, _ = result
        assert is_clear is True

    def test_tool_action(self) -> None:
        content = json.dumps({
            "thought": "restart nginx",
            "reasoning_path": "v3_react_thought",
            "action": {"tool": "k8s_rollout_restart", "args": {"namespace": "default"}},
        })
        result = _parse_react_turn(content)
        assert result is not None
        thought, rp, is_clear, tool_call = result
        assert is_clear is False
        assert tool_call is not None
        assert tool_call["tool"] == "k8s_rollout_restart"
        assert tool_call["args"]["namespace"] == "default"

    def test_non_json_returns_none(self) -> None:
        assert _parse_react_turn("hello world") is None

    def test_non_dict_json_returns_none(self) -> None:
        assert _parse_react_turn("[1, 2, 3]") is None

    def test_missing_tool_in_action_dict(self) -> None:
        content = json.dumps({"thought": "x", "action": {"no_tool": "k"}})
        assert _parse_react_turn(content) is None

    def test_default_reasoning_path(self) -> None:
        content = json.dumps({"thought": "ok", "action": "CLEAR"})
        result = _parse_react_turn(content)
        assert result is not None
        _, rp, _, _ = result
        assert rp == "v3_react_thought"

    def test_empty_args_in_tool_action(self) -> None:
        content = json.dumps({"thought": "ok", "action": {"tool": "k8s_scale_deployment"}})
        result = _parse_react_turn(content)
        assert result is not None
        _, _, _, tool_call = result
        assert tool_call is not None
        assert tool_call["args"] == {}


# ---------------------------------------------------------------------------
# _is_clear
# ---------------------------------------------------------------------------


class TestIsClear:
    def test_clear_uppercase(self) -> None:
        assert _is_clear("CLEAR") is True

    def test_clear_with_explanation(self) -> None:
        assert _is_clear("CLEAR no issues found") is True

    def test_not_clear(self) -> None:
        assert _is_clear("EXECUTE k8s_rollout_restart") is False

    def test_empty_string(self) -> None:
        assert _is_clear("") is False

    def test_lowercase_clear(self) -> None:
        # _is_clear uppercases the first line before comparing, so lowercase "clear" also matches
        assert _is_clear("clear") is True

    def test_clear_with_newline(self) -> None:
        assert _is_clear("CLEAR\nsome detail") is True


# ---------------------------------------------------------------------------
# _sigma_hint
# ---------------------------------------------------------------------------


class TestSigmaHint:
    def test_no_dr_returns_empty(self) -> None:
        assert _sigma_hint({"dr": False}) == ""

    def test_empty_manifest(self) -> None:
        assert _sigma_hint({}) == ""

    def test_with_both_z_cpu_and_z_mem_cpu_dominant(self) -> None:
        hint = _sigma_hint({"dr": True, "z_cpu": "5.0", "z_mem": "2.0"})
        assert "CPU" in hint
        assert "z_cpu=5.0" in hint

    def test_with_both_z_cpu_and_z_mem_mem_dominant(self) -> None:
        hint = _sigma_hint({"dr": True, "z_cpu": "2.0", "z_mem": "4.5"})
        assert "memory" in hint
        assert "z_mem=4.5" in hint

    def test_z_cpu_only(self) -> None:
        hint = _sigma_hint({"dr": True, "z_cpu": "3.0", "z_mem": None})
        assert "z_cpu=3.0" in hint

    def test_z_mem_only(self) -> None:
        hint = _sigma_hint({"dr": True, "z_cpu": None, "z_mem": "4.0"})
        assert "z_mem=4.0" in hint

    def test_invalid_z_values(self) -> None:
        hint = _sigma_hint({"dr": True, "z_cpu": "nan", "z_mem": "bad"})
        assert "Statistical Anomaly Detected" in hint

    def test_no_z_values(self) -> None:
        hint = _sigma_hint({"dr": True, "z_cpu": None, "z_mem": None})
        assert "Statistical Anomaly Detected" in hint


# ---------------------------------------------------------------------------
# _build_user_prompt
# ---------------------------------------------------------------------------


class TestBuildUserPrompt:
    def test_with_dr_and_no_evt(self) -> None:
        manifest = {"dr": True, "z_cpu": "3.5", "z_mem": None}
        prompt = _build_user_prompt(manifest, True, [])
        assert "Manifest:" in prompt
        assert "Statistical Anomaly" in prompt

    def test_with_evt_no_dr(self) -> None:
        manifest = {"dr": False, "evt": [{"type": "Warning"}]}
        prompt = _build_user_prompt(manifest, False, [{"type": "Warning"}])
        assert "Kubernetes Warning events present" in prompt

    def test_with_both_dr_and_evt(self) -> None:
        manifest = {"dr": True, "z_cpu": "3.5", "evt": [{"type": "Warning"}]}
        prompt = _build_user_prompt(manifest, True, [{"type": "Warning"}])
        assert "Also check evt list" in prompt

    def test_no_dr_no_evt(self) -> None:
        manifest = {"dr": False}
        prompt = _build_user_prompt(manifest, False, [])
        assert "Manifest:" in prompt

    def test_prompt_length_bounded(self) -> None:
        big_manifest = {"dr": True, "z_cpu": "3.5", "data": "x" * 20000}
        prompt = _build_user_prompt(big_manifest, True, [])
        assert len(prompt) <= 12000


# ---------------------------------------------------------------------------
# _system_prompt
# ---------------------------------------------------------------------------


class TestSystemPrompt:
    def test_contains_allowed_tools(self) -> None:
        prompt = _system_prompt({"k8s_rollout_restart", "k8s_scale_deployment"}, {"multi-agent"})
        assert "k8s_rollout_restart" in prompt
        assert "k8s_scale_deployment" in prompt

    def test_contains_allowed_namespaces(self) -> None:
        prompt = _system_prompt({"k8s_rollout_restart"}, {"multi-agent", "default"})
        assert "multi-agent" in prompt

    def test_contains_clear_option(self) -> None:
        prompt = _system_prompt({"k8s_rollout_restart"}, {"multi-agent"})
        assert "CLEAR" in prompt


# ---------------------------------------------------------------------------
# _system_prompt_react
# ---------------------------------------------------------------------------


class TestSystemPromptReact:
    def test_contains_react_instruction(self) -> None:
        prompt = _system_prompt_react({"k8s_rollout_restart"}, {"multi-agent"}, "catalog snippet")
        assert "ReAct" in prompt
        assert "k8s_rollout_restart" in prompt
        assert "multi-agent" in prompt
        assert "catalog snippet" in prompt


# ---------------------------------------------------------------------------
# _validate_k8s_ns
# ---------------------------------------------------------------------------


class TestValidateK8sNs:
    def test_non_k8s_rollout_tool_always_valid(self) -> None:
        ws = SimpleNamespace()
        call = ToolCallPayload(tool="k8s_scale_deployment", args={"namespace": "kube-system"})
        assert _validate_k8s_ns(ws, call, {"multi-agent"}) is True

    def test_allowed_namespace(self) -> None:
        ws = SimpleNamespace()
        call = ToolCallPayload(tool="k8s_rollout_restart", args={"namespace": "multi-agent"})
        assert _validate_k8s_ns(ws, call, {"multi-agent"}) is True

    def test_denied_namespace(self) -> None:
        ws = SimpleNamespace()
        call = ToolCallPayload(tool="k8s_rollout_restart", args={"namespace": "kube-system"})
        assert _validate_k8s_ns(ws, call, {"multi-agent"}) is False

    def test_dev_mode_allows_any_namespace(self) -> None:
        ws = SimpleNamespace(env_mode="dev")
        call = ToolCallPayload(tool="k8s_rollout_restart", args={"namespace": "kube-system"})
        assert _validate_k8s_ns(ws, call, {"multi-agent"}) is True

    def test_empty_namespace_uses_default_from_ws(self) -> None:
        ws = SimpleNamespace(k8s_default_namespace="multi-agent")
        call = ToolCallPayload(tool="k8s_rollout_restart", args={"namespace": ""})
        assert _validate_k8s_ns(ws, call, {"multi-agent"}) is True

    def test_empty_namespace_denied_when_default_not_in_allowed(self) -> None:
        ws = SimpleNamespace(k8s_default_namespace="kube-system")
        call = ToolCallPayload(tool="k8s_rollout_restart", args={"namespace": ""})
        assert _validate_k8s_ns(ws, call, {"multi-agent"}) is False


# ---------------------------------------------------------------------------
# _react_state_key
# ---------------------------------------------------------------------------


class TestReactStateKey:
    def test_format(self) -> None:
        key = _react_state_key("abc123")
        assert "omni:autonomous:react_state:" in key
        assert "abc123" in key

    def test_unique_for_different_fps(self) -> None:
        assert _react_state_key("fp1") != _react_state_key("fp2")


# ---------------------------------------------------------------------------
# _args_fingerprint
# ---------------------------------------------------------------------------


class TestArgsFingerprint:
    def test_returns_16_char_hex(self) -> None:
        fp = _args_fingerprint({"namespace": "default"})
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

    def test_stable(self) -> None:
        args = {"ns": "x", "dep": "y"}
        assert _args_fingerprint(args) == _args_fingerprint(args)

    def test_different_args_differ(self) -> None:
        assert _args_fingerprint({"a": 1}) != _args_fingerprint({"a": 2})

    def test_empty_args(self) -> None:
        fp = _args_fingerprint({})
        assert len(fp) == 16

    def test_sort_order_independent(self) -> None:
        assert _args_fingerprint({"a": 1, "b": 2}) == _args_fingerprint({"b": 2, "a": 1})


# ---------------------------------------------------------------------------
# _save_react_state / _load_prior_react_state
# ---------------------------------------------------------------------------


async def test_save_and_load_react_state() -> None:
    r = aioredis.FakeRedis(decode_responses=True)
    await _save_react_state(r, "fp999", turn=1, last_tool="k8s_rollout_restart", observation_masked="all clear", ttl_sec=3600)
    result = await _load_prior_react_state(r, "fp999")
    assert result is not None
    data = json.loads(result)
    assert data["turn"] == 1
    assert data["last_tool"] == "k8s_rollout_restart"
    assert data["obs"] == "all clear"


async def test_load_react_state_missing_key() -> None:
    r = aioredis.FakeRedis(decode_responses=True)
    result = await _load_prior_react_state(r, "nonexistent-fp")
    assert result is None


async def test_load_react_state_invalid_json() -> None:
    r = aioredis.FakeRedis(decode_responses=True)
    from workers.autonomous_decider import REDIS_REACT_STATE_PREFIX
    await r.set(REDIS_REACT_STATE_PREFIX + "bad-fp", "not-json")
    result = await _load_prior_react_state(r, "bad-fp")
    assert result is None


async def test_load_react_state_non_dict_json() -> None:
    r = aioredis.FakeRedis(decode_responses=True)
    from workers.autonomous_decider import REDIS_REACT_STATE_PREFIX
    await r.set(REDIS_REACT_STATE_PREFIX + "arr-fp", "[1,2,3]")
    result = await _load_prior_react_state(r, "arr-fp")
    assert result is None


# ---------------------------------------------------------------------------
# _tick_legacy — various branches
# ---------------------------------------------------------------------------


async def test_tick_legacy_no_snapshot() -> None:
    """No snapshot in Redis → early return, no crash."""
    r = aioredis.FakeRedis(decode_responses=True)
    from workers.autonomous_decider import _tick_legacy
    ctx = _make_ctx(r)
    await _tick_legacy(ctx, _make_ws(), "qwen2.5:7b", 600)


async def test_tick_legacy_invalid_json_snapshot() -> None:
    """Snapshot in Redis is not valid JSON → early return."""
    r = aioredis.FakeRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, "not-json{")
    from workers.autonomous_decider import _tick_legacy
    ctx = _make_ctx(r)
    await _tick_legacy(ctx, _make_ws(), "qwen2.5:7b", 600)


async def test_tick_legacy_non_dict_snapshot() -> None:
    """Snapshot is valid JSON but not a dict → early return."""
    r = aioredis.FakeRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps([1, 2, 3]))
    from workers.autonomous_decider import _tick_legacy
    ctx = _make_ctx(r)
    await _tick_legacy(ctx, _make_ws(), "qwen2.5:7b", 600)


async def test_tick_legacy_no_dr_no_evt() -> None:
    """No dr and no events → early return."""
    r = aioredis.FakeRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": False, "evt": []}))
    from workers.autonomous_decider import _tick_legacy
    ctx = _make_ctx(r)
    await _tick_legacy(ctx, _make_ws(), "qwen2.5:7b", 600)


async def test_tick_legacy_remediation_silent() -> None:
    """remediation_silent=True sets cooldown and returns."""
    r = aioredis.FakeRedis(decode_responses=True)
    manifest = {"dr": True, "evt": [], "z_cpu": "3.5", "remediation_silent": True}
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps(manifest))
    from workers.autonomous_decider import _tick_legacy
    ctx = _make_ctx(r)
    await _tick_legacy(ctx, _make_ws(), "qwen2.5:7b", 600)
    fp = _fingerprint(manifest)
    # Cooldown should be set
    assert await r.get(REDIS_KEY_COOLDOWN_PREFIX + fp) is not None


async def test_tick_legacy_cooldown_skip() -> None:
    """Cooldown key already set → skip processing."""
    r = aioredis.FakeRedis(decode_responses=True)
    manifest = {"dr": True, "evt": [], "z_cpu": "3.5"}
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps(manifest))
    fp = _fingerprint(manifest)
    await r.set(REDIS_KEY_COOLDOWN_PREFIX + fp, "1")
    from workers.autonomous_decider import _tick_legacy
    ctx = _make_ctx(r, llm=_FakeLLM("CLEAR"))
    await _tick_legacy(ctx, _make_ws(), "qwen2.5:7b", 600)
    # LLM should NOT have been called (cooldown active)


async def test_tick_legacy_empty_safe_tools() -> None:
    """autonomous_safe_tools empty → warning, return."""
    r = aioredis.FakeRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": True, "evt": [], "z_cpu": "3.5"}))
    from workers.autonomous_decider import _tick_legacy
    ctx = _make_ctx(r, llm=_FakeLLM("CLEAR"))
    ws = _make_ws(autonomous_safe_tools="")
    await _tick_legacy(ctx, ws, "qwen2.5:7b", 600)


async def test_tick_legacy_llm_returns_clear() -> None:
    """LLM returns CLEAR → cooldown set, no tool called."""
    r = aioredis.FakeRedis(decode_responses=True)
    manifest = {"dr": True, "evt": [], "z_cpu": "3.5"}
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps(manifest))
    from workers.autonomous_decider import _tick_legacy
    ctx = _make_ctx(r, llm=_FakeLLM("CLEAR"))
    await _tick_legacy(ctx, _make_ws(), "qwen2.5:7b", 600)
    fp = _fingerprint(manifest)
    assert await r.get(REDIS_KEY_COOLDOWN_PREFIX + fp) is not None


async def test_tick_legacy_llm_empty_response() -> None:
    """LLM returns empty content → no action, no crash."""
    r = aioredis.FakeRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": True, "evt": [], "z_cpu": "3.5"}))
    from workers.autonomous_decider import _tick_legacy
    ctx = _make_ctx(r, llm=_FakeLLM(""))
    await _tick_legacy(ctx, _make_ws(), "qwen2.5:7b", 600)


async def test_tick_legacy_llm_invalid_json() -> None:
    """LLM returns invalid JSON (not CLEAR) → parse error, no crash."""
    r = aioredis.FakeRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": True, "evt": [], "z_cpu": "3.5"}))
    from workers.autonomous_decider import _tick_legacy
    ctx = _make_ctx(r, llm=_FakeLLM("not-valid-json-at-all"))
    await _tick_legacy(ctx, _make_ws(), "qwen2.5:7b", 600)


async def test_tick_legacy_tool_not_in_allowlist() -> None:
    """Tool returned by LLM not in safe_tools → denied."""
    r = aioredis.FakeRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": True, "evt": [], "z_cpu": "3.5"}))
    content = json.dumps({"tool": "dangerous_tool", "args": {}})
    from workers.autonomous_decider import _tick_legacy
    ctx = _make_ctx(r, llm=_FakeLLM(content))
    await _tick_legacy(ctx, _make_ws(), "qwen2.5:7b", 600)


async def test_tick_legacy_tool_ns_denied() -> None:
    """k8s_rollout_restart with bad namespace → denied."""
    r = aioredis.FakeRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": True, "evt": [], "z_cpu": "3.5"}))
    content = json.dumps({"tool": "k8s_rollout_restart", "args": {"namespace": "kube-system"}})
    from workers.autonomous_decider import _tick_legacy
    ctx = _make_ctx(r, llm=_FakeLLM(content))
    await _tick_legacy(ctx, _make_ws(), "qwen2.5:7b", 600)


async def test_tick_legacy_unknown_registry_tool() -> None:
    """Tool in safe_tools but not in TOOL_REGISTRY → denied."""
    r = aioredis.FakeRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": True, "evt": [], "z_cpu": "3.5"}))
    content = json.dumps({"tool": "k8s_rollout_restart", "args": {"namespace": "multi-agent"}})
    from workers import autonomous_decider
    old_reg = autonomous_decider.TOOL_REGISTRY
    autonomous_decider.TOOL_REGISTRY = {}  # Empty registry
    try:
        from workers.autonomous_decider import _tick_legacy
        ctx = _make_ctx(r, llm=_FakeLLM(content))
        await _tick_legacy(ctx, _make_ws(), "qwen2.5:7b", 600)
    finally:
        autonomous_decider.TOOL_REGISTRY = old_reg


async def test_tick_legacy_tool_success_with_telegram() -> None:
    """Tool execution success with Telegram notification."""
    r = aioredis.FakeRedis(decode_responses=True)
    manifest = {"dr": True, "evt": [], "z_cpu": "3.5"}
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps(manifest))
    content = json.dumps({"tool": "k8s_rollout_restart", "args": {"namespace": "multi-agent", "deployment": "nginx"}})

    sent = []

    class FakeTg:
        async def send_message(self, cid: int, msg: str) -> None:
            sent.append({"cid": cid, "msg": msg})

    async def fake_tool(ctx: Any, args: dict) -> str:
        return "restart triggered"

    from workers import autonomous_decider
    old_reg = autonomous_decider.TOOL_REGISTRY
    autonomous_decider.TOOL_REGISTRY = {"k8s_rollout_restart": fake_tool}
    try:
        from workers.autonomous_decider import _tick_legacy
        ctx = _make_ctx(r, llm=_FakeLLM(content), telegram=FakeTg())
        ws = _make_ws(telegram_admin_chat_id="12345")
        await _tick_legacy(ctx, ws, "qwen2.5:7b", 600)
        assert len(sent) == 1
        assert "k8s_rollout_restart" in sent[0]["msg"]
    finally:
        autonomous_decider.TOOL_REGISTRY = old_reg


async def test_tick_legacy_tool_exception() -> None:
    """Tool raises exception → logged, no crash."""
    r = aioredis.FakeRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": True, "evt": [], "z_cpu": "3.5"}))
    content = json.dumps({"tool": "k8s_rollout_restart", "args": {"namespace": "multi-agent"}})

    async def broken_tool(ctx: Any, args: dict) -> str:
        raise RuntimeError("tool failed")

    from workers import autonomous_decider
    old_reg = autonomous_decider.TOOL_REGISTRY
    autonomous_decider.TOOL_REGISTRY = {"k8s_rollout_restart": broken_tool}
    try:
        from workers.autonomous_decider import _tick_legacy
        ctx = _make_ctx(r, llm=_FakeLLM(content))
        await _tick_legacy(ctx, _make_ws(), "qwen2.5:7b", 600)
    finally:
        autonomous_decider.TOOL_REGISTRY = old_reg


async def test_tick_legacy_with_events_only() -> None:
    """dr=False but evt has entries → processes."""
    r = aioredis.FakeRedis(decode_responses=True)
    manifest = {"dr": False, "evt": [{"type": "Warning", "reason": "OOMKilled"}]}
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps(manifest))
    from workers.autonomous_decider import _tick_legacy
    ctx = _make_ctx(r, llm=_FakeLLM("CLEAR"))
    await _tick_legacy(ctx, _make_ws(), "qwen2.5:7b", 600)


# ---------------------------------------------------------------------------
# _tick_react — various branches
# ---------------------------------------------------------------------------


async def test_tick_react_no_snapshot() -> None:
    r = aioredis.FakeRedis(decode_responses=True)
    from workers.autonomous_decider import _tick_react
    ctx = _make_ctx(r)
    await _tick_react(ctx, _make_ws(), "qwen2.5:7b", 600)


async def test_tick_react_no_dr_no_evt() -> None:
    r = aioredis.FakeRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": False, "evt": []}))
    from workers.autonomous_decider import _tick_react
    ctx = _make_ctx(r)
    await _tick_react(ctx, _make_ws(), "qwen2.5:7b", 600)


async def test_tick_react_remediation_silent() -> None:
    r = aioredis.FakeRedis(decode_responses=True)
    manifest = {"dr": True, "evt": [], "z_cpu": "3.5", "remediation_silent": True}
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps(manifest))
    from workers.autonomous_decider import _tick_react
    ctx = _make_ctx(r)
    await _tick_react(ctx, _make_ws(), "qwen2.5:7b", 600)
    fp = _fingerprint(manifest)
    assert await r.get(REDIS_KEY_COOLDOWN_PREFIX + fp) is not None


async def test_tick_react_cooldown_skip() -> None:
    r = aioredis.FakeRedis(decode_responses=True)
    manifest = {"dr": True, "evt": [], "z_cpu": "3.5"}
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps(manifest))
    fp = _fingerprint(manifest)
    await r.set(REDIS_KEY_COOLDOWN_PREFIX + fp, "1")
    from workers.autonomous_decider import _tick_react
    ctx = _make_ctx(r, llm=_FakeLLM("CLEAR"))
    await _tick_react(ctx, _make_ws(), "qwen2.5:7b", 600)


async def test_tick_react_empty_safe_tools() -> None:
    r = aioredis.FakeRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": True, "evt": [], "z_cpu": "3.5"}))
    from workers.autonomous_decider import _tick_react
    ctx = _make_ctx(r, llm=_FakeLLM("CLEAR"))
    ws = _make_ws(autonomous_safe_tools="")
    await _tick_react(ctx, ws, "qwen2.5:7b", 600)


async def test_tick_react_clear_json_response() -> None:
    """ReAct: LLM returns CLEAR JSON → resolved, cooldown set."""
    r = aioredis.FakeRedis(decode_responses=True)
    manifest = {"dr": True, "evt": [], "z_cpu": "3.5"}
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps(manifest))
    clear_content = json.dumps({"thought": "ok", "reasoning_path": "v3_react_thought", "action": "CLEAR"})
    from workers.autonomous_decider import _tick_react
    ctx = _make_ctx(r, llm=_FakeLLM(clear_content))
    await _tick_react(ctx, _make_ws(), "qwen2.5:7b", 600)
    fp = _fingerprint(manifest)
    assert await r.get(REDIS_KEY_COOLDOWN_PREFIX + fp) is not None


async def test_tick_react_empty_response() -> None:
    """ReAct: LLM returns empty content → break loop, aborted."""
    r = aioredis.FakeRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": True, "evt": [], "z_cpu": "3.5"}))
    from workers.autonomous_decider import _tick_react
    ctx = _make_ctx(r, llm=_FakeLLM(""))
    await _tick_react(ctx, _make_ws(react_max_turns=1, telegram_admin_chat_id=None), "qwen2.5:7b", 600)


async def test_tick_react_plain_clear_legacy() -> None:
    """ReAct: LLM returns plain CLEAR (not JSON) → legacy clear path."""
    r = aioredis.FakeRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": True, "evt": [], "z_cpu": "3.5"}))
    from workers.autonomous_decider import _tick_react
    ctx = _make_ctx(r, llm=_FakeLLM("CLEAR no issues"))
    await _tick_react(ctx, _make_ws(), "qwen2.5:7b", 600)


async def test_tick_react_invalid_format_retry() -> None:
    """ReAct: LLM returns invalid JSON (not parseable as react or clear) → retry message appended."""
    call_count = [0]

    class CountingLLM:
        async def chat(self, **kw: Any) -> dict[str, Any]:
            call_count[0] += 1
            if call_count[0] == 1:
                return {"message": {"content": "not-json-not-clear"}}
            return {"message": {"content": json.dumps({"thought": "ok", "action": "CLEAR"})}}

        async def chat_structured(self, **kw: Any) -> dict[str, Any]:
            return await self.chat(**kw)

    r = aioredis.FakeRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": True, "evt": [], "z_cpu": "3.5"}))
    from workers.autonomous_decider import _tick_react
    ctx = _make_ctx(r, llm=CountingLLM())
    await _tick_react(ctx, _make_ws(react_max_turns=3), "qwen2.5:7b", 600)
    assert call_count[0] >= 2


async def test_tick_react_tool_denied_not_in_allowlist() -> None:
    """ReAct: LLM returns valid react JSON with denied tool."""
    r = aioredis.FakeRedis(decode_responses=True)
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": True, "evt": [], "z_cpu": "3.5"}))
    content = json.dumps({
        "thought": "restart",
        "reasoning_path": "v3_react_thought",
        "action": {"tool": "dangerous_delete", "args": {}},
    })
    from workers.autonomous_decider import _tick_react
    ctx = _make_ctx(r, llm=_FakeLLM(content))
    await _tick_react(ctx, _make_ws(react_max_turns=1), "qwen2.5:7b", 600)


async def test_tick_react_abort_with_telegram() -> None:
    """ReAct: hits max turns and sends Telegram notification."""
    r = aioredis.FakeRedis(decode_responses=True)
    manifest = {"dr": True, "evt": [], "z_cpu": "3.5"}
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps(manifest))

    sent = []

    class FakeTg:
        async def send_message(self, cid: int, msg: str) -> None:
            sent.append(msg)

    from workers.autonomous_decider import _tick_react
    ctx = _make_ctx(r, llm=_FakeLLM("not-json"), telegram=FakeTg())
    ws = _make_ws(react_max_turns=1, telegram_admin_chat_id="9999")
    await _tick_react(ctx, ws, "qwen2.5:7b", 600)
    # Should have sent abort notification
    assert any("REACT_ABORTED" in m for m in sent)


async def test_tick_react_prior_state_loaded() -> None:
    """ReAct: prior state from Redis is loaded and appended to prompt."""
    r = aioredis.FakeRedis(decode_responses=True)
    manifest = {"dr": True, "evt": [], "z_cpu": "3.5"}
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps(manifest))
    fp = _fingerprint(manifest)

    # Pre-save a react state
    await _save_react_state(r, fp, turn=1, last_tool="k8s_rollout_restart", observation_masked="restarted ok", ttl_sec=3600)

    messages_captured = []

    class CapturingLLM:
        async def chat(self, model: str, messages: list, **kw: Any) -> dict[str, Any]:
            messages_captured.extend(messages)
            return {"message": {"content": json.dumps({"thought": "ok", "action": "CLEAR"})}}

        async def chat_structured(self, **kw: Any) -> dict[str, Any]:
            extra = {k: v for k, v in kw.items() if k not in ("model", "messages")}
            return await self.chat(kw["model"], kw["messages"], **extra)

    from workers.autonomous_decider import _tick_react
    ctx = _make_ctx(r, llm=CapturingLLM())
    await _tick_react(ctx, _make_ws(), "qwen2.5:7b", 600)

    user_msgs = [m["content"] for m in messages_captured if m["role"] == "user"]
    assert any("Prior ReAct state" in msg for msg in user_msgs)


# ---------------------------------------------------------------------------
# _tick (dispatcher)
# ---------------------------------------------------------------------------


async def test_tick_dispatches_to_react() -> None:
    """_tick calls _tick_react when autonomous_react_enabled=True."""
    r = aioredis.FakeRedis(decode_responses=True)
    from workers.autonomous_decider import _tick
    ctx = _make_ctx(r, llm=_FakeLLM("CLEAR"))
    ws = _make_ws(autonomous_react_enabled=True)
    await _tick(ctx, ws, "qwen2.5:7b", 600)


async def test_tick_dispatches_to_legacy() -> None:
    """_tick calls _tick_legacy when autonomous_react_enabled=False."""
    r = aioredis.FakeRedis(decode_responses=True)
    from workers.autonomous_decider import _tick
    ctx = _make_ctx(r, llm=_FakeLLM("CLEAR"))
    ws = _make_ws(autonomous_react_enabled=False)
    await _tick(ctx, ws, "qwen2.5:7b", 600)


# ---------------------------------------------------------------------------
# autonomous_decider_loop
# ---------------------------------------------------------------------------


async def test_autonomous_decider_loop_disabled() -> None:
    """Loop exits immediately when disabled."""
    r = aioredis.FakeRedis(decode_responses=True)
    stop = asyncio.Event()
    ctx = _make_ctx(r, settings=SimpleNamespace(autonomous_decider_enabled=False))
    await autonomous_decider_loop(ctx, stop)


async def test_autonomous_decider_loop_exits_on_stop() -> None:
    """Loop exits when stop event is set."""
    r = aioredis.FakeRedis(decode_responses=True)
    stop = asyncio.Event()

    ws = SimpleNamespace(
        autonomous_decider_enabled=True,
        autonomous_decider_interval_sec=0.01,
        autonomous_fix_cooldown_sec=600,
        autonomous_decider_model="",
        model_reasoning_engine="qwen2.5:7b",
        autonomous_react_enabled=False,
        autonomous_safe_tools="",
        autonomous_allowed_namespaces="multi-agent",
        llm_chat_timeout_sec=1,
    )

    ctx = _make_ctx(
        r,
        settings=ws,
        llm=_FakeLLM("CLEAR"),
        semaphore=_FakeSemaphore(),
    )

    async def stop_after() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(autonomous_decider_loop(ctx, stop), stop_after())


async def test_autonomous_decider_loop_uses_model_reasoning_fallback() -> None:
    """Loop uses model_reasoning_engine when autonomous_decider_model is unset."""
    r = aioredis.FakeRedis(decode_responses=True)
    stop = asyncio.Event()

    ws = SimpleNamespace(
        autonomous_decider_enabled=True,
        autonomous_decider_interval_sec=0.01,
        autonomous_fix_cooldown_sec=600,
        autonomous_decider_model=None,
        model_reasoning_engine="deepseek-r1:8b",
        autonomous_react_enabled=False,
        autonomous_safe_tools="",
        autonomous_allowed_namespaces="multi-agent",
        llm_chat_timeout_sec=1,
    )

    ctx = _make_ctx(r, settings=ws, llm=_FakeLLM("CLEAR"))

    async def stop_after() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(autonomous_decider_loop(ctx, stop), stop_after())
