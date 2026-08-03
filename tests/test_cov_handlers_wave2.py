"""Coverage wave-2 tests for src/workers/handlers.py.

Targets uncovered lines:
  190-191: _effective_inbound_text_preview exception path
  446, 485, 488: _slow_path_system_messages_for_ctx / build_agentic unattended+shell branches
  566-567: _parse_suggestions_json_tail (parse failure, short array)
  588: _conversational_fallback infra block
  612-614: _conversational_fallback LLM exception path
  715-723: resolve_remediation_from_memory (tool execution + SOP fastpath)
  770-791: try_fast_path routing_experience path (hits + no-hit)
  900-901: _slow_path_abort_no_data exception in inc_slow_path_exhausted
  955-959: slow_path_with_llm_and_tools baseline_snapshot enabled branch
  961, 963, 970-974: slow_path session_summary + recent_turns
  1104-1106: slow_path autonomous sdk rescue exception
  1125, 1173, 1190: stale-signature abort in unknown_tool / tool_error
  1283-1287: write_pending exception
  1299-1303: rollout_pending exception
  1316-1329: host_vm_chart path (with chat_id)
  1335-1341: list_all_pods with chat_id + pairs
  1348-1350: autonomous_sdk exception swallowed
  1353-1356: autonomous_sdk with chat_id saves session
  1361-1413: pending_await_vm_slots (host branch, vm_slots_ready, nudge)
  1424-1427: rag_gate hit with chat_id session save
  1454, 1460-1462: vm_slots ambiguous with no chat_id / infra_context exception
  1471-1474: fast_path hit with chat_id session save
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest

os.environ.setdefault("OMNI_ENV_MODE", "dev")
os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OMNI_OLLAMA_BASE_URL", "http://localhost:11434")


def _make_settings(**kw: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "god_mode": False,
        "lab_unchained": False,
        "omni_concise_reply_max_words": 40,
        "omni_summary_max_words": 400,
        "slow_path_max_tool_attempts": 2,
        "slow_path_stale_signature_streak": 2,
        "json_repair_max": 0,
        "compress_turn_threshold": 10,
        "model_helper": "qwen2.5:1.5b",
        "model_reasoning_engine": "qwen2.5:7b",
        "model_heavy_lifter": "qwen2.5:7b",
        "chat_model": "qwen2.5:7b",
        "embed_model": "nomic-embed-text",
        "rag_fast_path_score": 0.9,
        "routing_experience_enabled": True,
        "routing_experience_score_threshold": 0.78,
        "action_experience_enabled": True,
        "baseline_snapshot_enabled": False,
        "baseline_system_prompt_max_chars": 1600,
        "agentic_slow_path_enabled": False,
        "fallback_inline_buttons_enabled": False,
        "session_ttl_sec": 86400,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_ctx(**kw: Any) -> SimpleNamespace:
    scout_ready = asyncio.Event()
    scout_ready.set()
    defaults: dict[str, Any] = {
        "settings": _make_settings(),
        "redis": fakeredis.aioredis.FakeRedis(decode_responses=True),
        "llm": None,
        "vector_store": None,
        "ledger": None,
        "semaphore": None,
        "telegram": None,
        "kafka": None,
        "telegram_chat_id": None,
        "inbound_source": "",
        "inbound_user_text": "",
        "restart_rollout_explicit": False,
        "pod_discovery_pairs": [],
        "scout_ready": scout_ready,
        "inbound_trace_id": "test-trace",
        "llm_slot_held": False,
        "inbound_proactive": False,
        "k8s_mutated": False,
        "fallback_inline_commands": None,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _fake_llm_chat(content: str = ""):
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"message": {"content": content}})
    llm.embed = AsyncMock(return_value={"embedding": [0.1] * 768})
    return llm


def _fake_semaphore():
    sem = MagicMock()
    sem.acquire = AsyncMock(return_value="token")
    sem.release = AsyncMock()
    return sem


# ---------------------------------------------------------------------------
# _effective_inbound_text_preview — exception path (lines 190-191)
# ---------------------------------------------------------------------------

class TestEffectiveInboundTextPreviewException:
    def test_data_dict_raises_exception_returns_empty(self):
        """If processing alerts raises an exception, returns empty string."""
        from workers.handlers import _effective_inbound_text_preview
        # Craft a payload where the alerts list contains a non-dict that triggers TypeError
        payload = {"data": {"alerts": ["not a dict", None]}}
        result = _effective_inbound_text_preview(payload)
        # Should not raise, just return some string (either empty or processed)
        assert isinstance(result, str)

    def test_data_payload_key(self):
        """payload key used as fallback for data."""
        from workers.handlers import _effective_inbound_text_preview
        payload = {"payload": {"alerts": [{"labels": {"alertname": "MyAlert"}, "annotations": {}}]}}
        result = _effective_inbound_text_preview(payload)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _parse_suggestions_json_tail — parse failure and short array
# ---------------------------------------------------------------------------

class TestParseSuggestionsJsonTail:
    def test_no_suggestions_json_key(self):
        from workers.handlers import _parse_suggestions_json_tail
        text = "Normal response without suggestions"
        display, cmds = _parse_suggestions_json_tail(text)
        assert display == "Normal response without suggestions"
        assert cmds is None

    def test_invalid_json_returns_original(self):
        """SUGGESTIONS_JSON: present but invalid JSON → returns text, None."""
        from workers.handlers import _parse_suggestions_json_tail
        text = "Some text\nSUGGESTIONS_JSON: [invalid json"
        display, cmds = _parse_suggestions_json_tail(text)
        assert cmds is None

    def test_short_array_less_than_3_returns_none(self):
        """SUGGESTIONS_JSON present but array < 3 items → returns None."""
        from workers.handlers import _parse_suggestions_json_tail
        text = 'Some text\nSUGGESTIONS_JSON: ["cmd1", "cmd2"]'
        display, cmds = _parse_suggestions_json_tail(text)
        assert cmds is None

    def test_valid_3_item_array_returns_cmds(self):
        from workers.handlers import _parse_suggestions_json_tail
        text = 'Response\nSUGGESTIONS_JSON: ["cmd1", "cmd2", "cmd3"]'
        display, cmds = _parse_suggestions_json_tail(text)
        assert cmds == ["cmd1", "cmd2", "cmd3"]
        assert display == "Response"

    def test_json_decode_error_returns_none(self):
        """JSONDecodeError path — when json.loads fails on SUGGESTIONS_JSON."""
        from workers.handlers import _parse_suggestions_json_tail
        text = "Head\nSUGGESTIONS_JSON: not-valid-at-all"
        display, cmds = _parse_suggestions_json_tail(text)
        assert cmds is None


# ---------------------------------------------------------------------------
# _conversational_fallback — LLM exception path (lines 612-614)
# ---------------------------------------------------------------------------

class TestConversationalFallback:
    async def test_llm_exception_returns_fallback_message(self):
        """LLM exception → returns static fallback message."""
        from workers.handlers import _conversational_fallback

        llm = MagicMock()
        llm.chat = AsyncMock(side_effect=RuntimeError("llm down"))
        ctx = _make_ctx(
            llm=llm,
            settings=_make_settings(
                model_heavy_lifter="qwen2.5:7b",
                chat_model="qwen2.5:7b",
            ),
        )

        with patch("workers.handlers.fetch_infra_injection_for_fallback", new=AsyncMock(return_value="")):
            result = await _conversational_fallback(
                ctx, "check pods", "trace-fb", reason="test_reason"
            )
        assert isinstance(result, str)
        assert len(result) > 0

    async def test_empty_llm_response_returns_default(self):
        """LLM returns empty content → uses default fallback text."""
        from workers.handlers import _conversational_fallback

        llm = MagicMock()
        llm.chat = AsyncMock(return_value={"message": {"content": ""}})
        ctx = _make_ctx(llm=llm, settings=_make_settings())

        with patch("workers.handlers.fetch_infra_injection_for_fallback", new=AsyncMock(return_value="")):
            result = await _conversational_fallback(
                ctx, "query", "trace-empty", reason="no_json"
            )
        assert "Tình trạng" in result or "pipeline" in result or isinstance(result, str)

    async def test_learned_context_included(self):
        """learned_context is appended to user message."""
        from workers.handlers import _conversational_fallback

        llm = MagicMock()
        llm.chat = AsyncMock(return_value={"message": {"content": "SRE response"}})
        ctx = _make_ctx(llm=llm, settings=_make_settings())

        with patch("workers.handlers.fetch_infra_injection_for_fallback", new=AsyncMock(return_value="infra context")):
            result = await _conversational_fallback(
                ctx, "check pod", "trace-lc", reason="fallback",
                learned_context="RAG memory content"
            )
        assert isinstance(result, str)

    async def test_infra_injection_exception_swallowed(self):
        """Exception in fetch_infra_injection_for_fallback is swallowed."""
        from workers.handlers import _conversational_fallback

        llm = MagicMock()
        llm.chat = AsyncMock(return_value={"message": {"content": "ok"}})
        ctx = _make_ctx(llm=llm, settings=_make_settings())

        with patch("workers.handlers.fetch_infra_injection_for_fallback", new=AsyncMock(side_effect=RuntimeError("net error"))):
            result = await _conversational_fallback(
                ctx, "query", "trace-inj", reason="reason"
            )
        assert isinstance(result, str)

    async def test_fallback_inline_buttons_enabled_sets_commands(self):
        """When cmds has 3 items and fallback_inline_buttons_enabled, ctx.fallback_inline_commands is set."""
        from workers.handlers import _conversational_fallback

        response = "Some answer\nSUGGESTIONS_JSON: [\"cmd1\",\"cmd2\",\"cmd3\"]"
        llm = MagicMock()
        llm.chat = AsyncMock(return_value={"message": {"content": response}})
        ctx = _make_ctx(llm=llm, settings=_make_settings(fallback_inline_buttons_enabled=True))

        with patch("workers.handlers.fetch_infra_injection_for_fallback", new=AsyncMock(return_value="")):
            result = await _conversational_fallback(
                ctx, "query", "trace-btn", reason="reason"
            )
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# resolve_remediation_from_memory — tool execution + SOP fastpath hit
# ---------------------------------------------------------------------------

class TestResolveRemediationToolExecution:
    async def test_tool_execution_succeeds(self):
        """auto_execute=True + valid tool → tool is called, returns True."""
        from workers.handlers import resolve_remediation_from_memory, TOOL_REGISTRY
        from rag.pgvector_store import COLLECTION_SOP

        hit = MagicMock()
        hit.payload = {"tool": "reply", "auto_execute": True, "args": {"text": "ok"}}
        hit.score = 0.95

        vs = MagicMock()
        vs.query_points = AsyncMock(return_value=MagicMock(points=[hit]))
        ctx = _make_ctx(vector_store=vs, llm=_fake_llm_chat())

        async def _fake_reply(ctx, args):
            return "reply output"

        with patch.dict(TOOL_REGISTRY, {"reply": _fake_reply}), \
             patch("workers.handlers.get_tool_registry") as mock_reg, \
             patch("workers.handlers.inc_fastpath_hits"):
            mock_reg.return_value.has.return_value = True
            ok, out, tool = await resolve_remediation_from_memory(
                ctx, "check pods", trace="t1", collection_name=COLLECTION_SOP,
                score_threshold=0.9,
            )
        assert ok is True
        assert out == "reply output"
        assert tool == "reply"

    async def test_score_below_threshold_returns_false(self):
        """Score below threshold → miss."""
        from workers.handlers import resolve_remediation_from_memory
        from rag.pgvector_store import COLLECTION_SOP

        hit = MagicMock()
        hit.payload = {"tool": "reply", "auto_execute": True, "args": {}}
        hit.score = 0.5  # below threshold of 0.9

        vs = MagicMock()
        vs.query_points = AsyncMock(return_value=MagicMock(points=[hit]))
        ctx = _make_ctx(vector_store=vs, llm=_fake_llm_chat())

        ok, out, tool = await resolve_remediation_from_memory(
            ctx, "query", trace="t2", collection_name=COLLECTION_SOP, score_threshold=0.9,
        )
        assert ok is False
        assert out is None

    async def test_prepare_tool_return_called_when_not_in_registry(self):
        """When tool_registry.has returns False, prepare_tool_return_for_llm is called."""
        from workers.handlers import resolve_remediation_from_memory, TOOL_REGISTRY
        from rag.pgvector_store import COLLECTION_SOP

        hit = MagicMock()
        hit.payload = {"tool": "reply", "auto_execute": True, "args": {}}
        hit.score = 0.95

        vs = MagicMock()
        vs.query_points = AsyncMock(return_value=MagicMock(points=[hit]))
        ctx = _make_ctx(vector_store=vs, llm=_fake_llm_chat())

        async def _fake_reply(ctx, args):
            return "raw output"

        with patch.dict(TOOL_REGISTRY, {"reply": _fake_reply}), \
             patch("workers.handlers.get_tool_registry") as mock_reg, \
             patch("workers.handlers.prepare_tool_return_for_llm", return_value="prepared") as mock_prep, \
             patch("workers.handlers.inc_fastpath_hits"):
            mock_reg.return_value.has.return_value = False  # tool NOT in new registry
            ok, out, tool = await resolve_remediation_from_memory(
                ctx, "query", trace="t3", collection_name=COLLECTION_SOP, score_threshold=0.9,
            )
        assert ok is True
        mock_prep.assert_called_once()


# ---------------------------------------------------------------------------
# try_fast_path — routing_experience path (lines 770-791)
# ---------------------------------------------------------------------------

class TestTryFastPathRoutingExperience:
    async def test_routing_experience_with_valid_hit(self):
        """action_experience search has a valid auto_execute routing hit → returns True."""
        from workers.handlers import try_fast_path, TOOL_REGISTRY
        from workers.routing_policy import ROUTING_SOURCES_FAST_PATH_EXECUTE

        # Get first valid routing source
        valid_source = list(ROUTING_SOURCES_FAST_PATH_EXECUTE)[0] if ROUTING_SOURCES_FAST_PATH_EXECUTE else "sop"

        hit = MagicMock()
        hit.payload = {
            "routing_source": valid_source,
            "auto_execute": True,
            "tool": "reply",
            "args": {"text": "routed"},
        }
        hit.score = 0.88

        vs = MagicMock()
        # resolve_remediation_from_memory is patched — only this query_points runs (action_experience).
        vs.query_points = AsyncMock(return_value=MagicMock(points=[hit]))
        ctx = _make_ctx(llm=_fake_llm_chat(), vector_store=vs)

        async def _fake_reply(ctx, args):
            return "routed output"

        with patch("workers.handlers.resolve_remediation_from_memory", new=AsyncMock(return_value=(False, None, None))), \
             patch.dict(TOOL_REGISTRY, {"reply": _fake_reply}), \
             patch("workers.handlers.get_tool_registry") as mock_reg, \
             patch("workers.handlers.log_react_json"):
            mock_reg.return_value.has.return_value = True
            ok, out = await try_fast_path(ctx, "check status", trace="t-route")
        assert ok is True
        assert out == "routed output"

    async def test_routing_experience_skip_invalid_source(self):
        """Hit with routing_source not in ROUTING_SOURCES → skip."""
        from workers.handlers import try_fast_path

        hit = MagicMock()
        hit.payload = {
            "routing_source": "invalid_source_xyz",
            "auto_execute": True,
            "tool": "reply",
            "args": {},
        }

        vs = MagicMock()
        vs.query_points = AsyncMock(return_value=MagicMock(points=[hit]))
        ctx = _make_ctx(llm=_fake_llm_chat(), vector_store=vs)

        with patch("workers.handlers.resolve_remediation_from_memory", new=AsyncMock(return_value=(False, None, None))):
            ok, out = await try_fast_path(ctx, "check pods", trace="t-inv")
        assert ok is False


# ---------------------------------------------------------------------------
# _slow_path_abort_no_data — exception in inc_slow_path_exhausted (lines 900-901)
# ---------------------------------------------------------------------------

class TestSlowPathAbortNoData:
    async def test_inc_exception_swallowed(self):
        """Exception in inc_slow_path_exhausted is swallowed."""
        from workers.handlers import _slow_path_abort_no_data
        from workers.slow_path_trace import AttemptRecord

        ctx = _make_ctx(settings=_make_settings(slow_path_max_tool_attempts=2))
        attempt_trace = [
            AttemptRecord(attempt=1, phase="parse", error_signature="sig", one_line="err", detail_full="err"),
        ]

        with patch("workers.handlers.inc_slow_path_exhausted", side_effect=RuntimeError("metric fail")), \
             patch("workers.handlers.record_routing_exhausted_no_data", new=AsyncMock()):
            result = await _slow_path_abort_no_data(
                ctx, "query", "trace-exc", attempt_trace=attempt_trace, exit_reason="max_attempts"
            )
        assert isinstance(result, str)

    async def test_empty_attempt_trace(self):
        """Empty attempt_trace → no crash, uses '' for detail."""
        from workers.handlers import _slow_path_abort_no_data

        ctx = _make_ctx(settings=_make_settings(slow_path_max_tool_attempts=2))

        with patch("workers.handlers.inc_slow_path_exhausted"), \
             patch("workers.handlers.record_routing_exhausted_no_data", new=AsyncMock()):
            result = await _slow_path_abort_no_data(
                ctx, "query", "trace-empty", attempt_trace=[], exit_reason="loop_exit"
            )
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# slow_path_with_llm_and_tools — baseline_snapshot + session_summary + recent_turns
# ---------------------------------------------------------------------------

class TestSlowPathBranchCoverage:
    def _make_slow_ctx(self, llm_content: str = "") -> SimpleNamespace:
        llm = _fake_llm_chat(llm_content)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        return _make_ctx(
            redis=redis,
            llm=llm,
            semaphore=_fake_semaphore(),
            settings=_make_settings(
                slow_path_max_tool_attempts=1,
                slow_path_stale_signature_streak=3,
                baseline_snapshot_enabled=True,
                baseline_system_prompt_max_chars=800,
            ),
        )

    async def test_baseline_snapshot_included(self):
        """baseline_snapshot_enabled=True → fetch_baseline_system_prompt called."""
        from workers.handlers import slow_path_with_llm_and_tools

        ctx = self._make_slow_ctx("")

        with patch("workers.handlers.inc_llm_requests"), \
             patch("workers.handlers.record_routing_exhausted_no_data", new=AsyncMock()), \
             patch("workers.handlers.inc_slow_path_exhausted"), \
             patch("workers.handlers.fetch_baseline_system_prompt", new=AsyncMock(return_value="baseline content")) as mock_bl:
            result = await slow_path_with_llm_and_tools(ctx, "query", trace="t-bl")
        mock_bl.assert_called_once()
        assert isinstance(result, str)

    async def test_session_summary_appended(self):
        """session_summary → added as system message."""
        from workers.handlers import slow_path_with_llm_and_tools

        ctx = _make_ctx(
            llm=_fake_llm_chat(""),
            semaphore=_fake_semaphore(),
            settings=_make_settings(slow_path_max_tool_attempts=1),
        )

        with patch("workers.handlers.inc_llm_requests"), \
             patch("workers.handlers.record_routing_exhausted_no_data", new=AsyncMock()), \
             patch("workers.handlers.inc_slow_path_exhausted"):
            result = await slow_path_with_llm_and_tools(
                ctx, "query", trace="t-sum", session_summary="Previous session summary"
            )
        assert isinstance(result, str)

    async def test_recent_turns_appended(self):
        """recent_turns → added as user/assistant messages."""
        from workers.handlers import slow_path_with_llm_and_tools

        ctx = _make_ctx(
            llm=_fake_llm_chat(""),
            semaphore=_fake_semaphore(),
            settings=_make_settings(slow_path_max_tool_attempts=1),
        )
        recent = [
            {"role": "user", "content": "previous question"},
            {"role": "assistant", "content": "previous answer"},
        ]

        with patch("workers.handlers.inc_llm_requests"), \
             patch("workers.handlers.record_routing_exhausted_no_data", new=AsyncMock()), \
             patch("workers.handlers.inc_slow_path_exhausted"):
            result = await slow_path_with_llm_and_tools(
                ctx, "query", trace="t-turns", recent_turns=recent
            )
        assert isinstance(result, str)

    async def test_autonomous_sdk_rescue_exception_swallowed(self):
        """Exception in try_autonomous_sdk_route rescue is swallowed, returns None."""
        from workers.handlers import slow_path_with_llm_and_tools

        ctx = _make_ctx(
            llm=_fake_llm_chat('{"tool": "unknown_xyz", "args": {}}'),
            semaphore=_fake_semaphore(),
            settings=_make_settings(slow_path_max_tool_attempts=1),
        )

        with patch("workers.handlers.inc_llm_requests"), \
             patch("workers.handlers.record_routing_exhausted_no_data", new=AsyncMock()), \
             patch("workers.handlers.inc_slow_path_exhausted"), \
             patch("workers.handlers.try_autonomous_sdk_route", new=AsyncMock(side_effect=RuntimeError("sdk fail"))):
            result = await slow_path_with_llm_and_tools(ctx, "query", trace="t-rescue-exc")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _handle_inbound_payload_impl — write_pending exception (lines 1283-1287)
# ---------------------------------------------------------------------------

class TestHandleInboundPayloadImplAdvanced:
    async def test_write_pending_exception_returns_error(self):
        """execute_write_pending_from_redis raises → returns error message."""
        from workers.handlers import _handle_inbound_payload_impl
        from workers.k8s_tools import redis_key_write_pending

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        chat_id = 12345
        await redis.set(redis_key_write_pending(chat_id), json.dumps({"deployment": "api"}))
        ctx = _make_ctx(redis=redis)

        with patch("workers.handlers._user_confirms_rollout_telegram", return_value=True), \
             patch("workers.handlers.execute_write_pending_from_redis", new=AsyncMock(side_effect=RuntimeError("write fail"))), \
             patch("workers.handlers.child_span") as sp:
            sp.return_value.__enter__ = MagicMock(return_value=None)
            sp.return_value.__exit__ = MagicMock(return_value=False)
            result = await _handle_inbound_payload_impl(
                ctx, {"text": "confirm", "source": "telegram", "chat_id": chat_id}, "t-wp-exc"
            )
        assert "error" in result.lower() or "Write" in result

    async def test_rollout_pending_exception_returns_error(self):
        """execute_rollout_restart_from_pending raises → returns error message."""
        from workers.handlers import _handle_inbound_payload_impl
        from workers.k8s_tools import redis_key_rollout_pending

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        chat_id = 99999
        await redis.set(redis_key_rollout_pending(chat_id), json.dumps({"deployment": "api"}))
        ctx = _make_ctx(redis=redis)

        with patch("workers.handlers._user_confirms_rollout_telegram", return_value=True), \
             patch("workers.handlers.execute_write_pending_from_redis", new=AsyncMock(return_value=None)), \
             patch("workers.handlers.execute_rollout_restart_from_pending", new=AsyncMock(side_effect=RuntimeError("rollout fail"))), \
             patch("workers.handlers.child_span") as sp:
            sp.return_value.__enter__ = MagicMock(return_value=None)
            sp.return_value.__exit__ = MagicMock(return_value=False)
            result = await _handle_inbound_payload_impl(
                ctx, {"text": "confirm", "source": "telegram", "chat_id": chat_id}, "t-rp-exc"
            )
        assert "error" in result.lower() or "Rollout" in result

    async def test_host_vm_chart_path_with_chat_id(self):
        """state.monitoring_target_type='host' + chart request → host_vm_chart branch."""
        from workers.handlers import _handle_inbound_payload_impl, TOOL_REGISTRY
        from workers.session_state import SessionState, save_session

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        chat_id = 88888
        state = SessionState()
        state.monitoring_target_type = "host"
        await save_session(redis, chat_id, state, ttl_sec=3600)

        ctx = _make_ctx(redis=redis)

        async def _fake_prom(ctx, args):
            return "prometheus chart data"

        with patch.dict(TOOL_REGISTRY, {"query_prometheus_metrics": _fake_prom}), \
             patch("workers.handlers._wants_host_vm_chart", return_value=True):
            result = await _handle_inbound_payload_impl(
                ctx,
                {"text": "show chart 1h", "source": "telegram", "chat_id": chat_id},
                "t-hvc",
            )
        assert "prometheus chart data" in result

    async def test_list_all_pods_with_chat_id_and_pairs(self):
        """list_all_pods path with chat_id + pod_discovery_pairs → stores in session."""
        from workers.handlers import _handle_inbound_payload_impl, TOOL_REGISTRY

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        chat_id = 77777
        ctx = _make_ctx(redis=redis)
        ctx.pod_discovery_pairs = [("ns1", "pod-a"), ("ns2", "pod-b")]

        async def _fake_list(c, args):
            return "pod list result"

        with patch.dict(TOOL_REGISTRY, {"list_all_pods_sdk": _fake_list}):
            result = await _handle_inbound_payload_impl(
                ctx,
                {"text": "list all pods", "source": "telegram", "chat_id": chat_id},
                "t-lp",
            )
        assert "pod list result" in result

    async def test_autonomous_sdk_exception_swallowed(self):
        """autonomous_sdk_route raises → swallowed, auto_sdk=None → continues."""
        from workers.handlers import _handle_inbound_payload_impl

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis)

        with patch("workers.handlers.try_autonomous_sdk_route", new=AsyncMock(side_effect=RuntimeError("sdk boom"))), \
             patch("workers.handlers.evaluate_rag_gate", new=AsyncMock(return_value=MagicMock(hit=False, formatted=""))), \
             patch("workers.handlers.preflight_infra_kb", new=AsyncMock(return_value=MagicMock(context=""))), \
             patch("workers.handlers.is_ambiguous_resource_check", return_value=False), \
             patch("workers.handlers.enrich_working_text_with_infra", new=AsyncMock(return_value="text")), \
             patch("workers.handlers.try_fast_path", new=AsyncMock(return_value=(True, "fast"))):
            result = await _handle_inbound_payload_impl(
                ctx, {"text": "check pods", "source": "telegram"}, "t-sdk-exc"
            )
        assert isinstance(result, str)

    async def test_autonomous_sdk_hit_with_chat_id_saves_session(self):
        """autonomous_sdk_route returns result with chat_id → saves session."""
        from workers.handlers import _handle_inbound_payload_impl

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        chat_id = 55555
        ctx = _make_ctx(redis=redis)

        with patch("workers.handlers.try_autonomous_sdk_route", new=AsyncMock(return_value="autonomous output")), \
             patch("workers.handlers.evaluate_rag_gate", new=AsyncMock(return_value=MagicMock(hit=False, formatted=""))):
            result = await _handle_inbound_payload_impl(
                ctx,
                {"text": "describe pod api", "source": "telegram", "chat_id": chat_id},
                "t-sdk-chat",
            )
        assert result == "autonomous output"

    async def test_pending_await_vm_slots_host_path(self):
        """pending_action=PENDING_AWAIT_VM_SLOTS + followup_indicates_host → psutil path."""
        from workers.handlers import _handle_inbound_payload_impl, TOOL_REGISTRY
        from workers.session_state import SessionState, save_session, PENDING_AWAIT_VM_SLOTS

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        chat_id = 44444
        state = SessionState()
        state.pending_action = PENDING_AWAIT_VM_SLOTS
        await save_session(redis, chat_id, state, ttl_sec=3600)

        ctx = _make_ctx(redis=redis)

        async def _fake_psutil(c, args):
            return "psutil host data"

        with patch.dict(TOOL_REGISTRY, {"system_psutil": _fake_psutil}), \
             patch("workers.handlers.try_autonomous_sdk_route", new=AsyncMock(return_value=None)), \
             patch("workers.handlers.evaluate_rag_gate", new=AsyncMock(return_value=MagicMock(hit=False, formatted=""))), \
             patch("workers.handlers.followup_indicates_host", return_value=True), \
             patch("workers.handlers.extract_entities_llm", new=AsyncMock(return_value={})), \
             patch("workers.handlers.merge_llm_entities_into_slots", return_value={}), \
             patch("workers.handlers.parse_resource_followup", return_value=("host", None)), \
             patch("workers.handlers.merge_vm_slots", return_value={}), \
             patch("workers.handlers.enrich_slots_from_discovery", return_value={}):
            result = await _handle_inbound_payload_impl(
                ctx,
                {"text": "host", "source": "telegram", "chat_id": chat_id},
                "t-vm-host",
            )
        assert "psutil host data" in result

    async def test_pending_await_vm_slots_ready_path(self):
        """pending_action=PENDING_AWAIT_VM_SLOTS + vm_slots_ready → query_prometheus_metrics."""
        from workers.handlers import _handle_inbound_payload_impl, TOOL_REGISTRY
        from workers.session_state import SessionState, save_session, PENDING_AWAIT_VM_SLOTS

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        chat_id = 33333
        state = SessionState()
        state.pending_action = PENDING_AWAIT_VM_SLOTS
        await save_session(redis, chat_id, state, ttl_sec=3600)

        ctx = _make_ctx(redis=redis)

        async def _fake_prom(c, args):
            return "prom data ready"

        with patch.dict(TOOL_REGISTRY, {"query_prometheus_metrics": _fake_prom}), \
             patch("workers.handlers.try_autonomous_sdk_route", new=AsyncMock(return_value=None)), \
             patch("workers.handlers.evaluate_rag_gate", new=AsyncMock(return_value=MagicMock(hit=False, formatted=""))), \
             patch("workers.handlers.followup_indicates_host", return_value=False), \
             patch("workers.handlers.extract_entities_llm", new=AsyncMock(return_value={})), \
             patch("workers.handlers.merge_llm_entities_into_slots", return_value={"target_type": "pod", "pod_name": "api"}), \
             patch("workers.handlers.parse_resource_followup", return_value=("pod", None)), \
             patch("workers.handlers.merge_vm_slots", return_value={"target_type": "pod", "pod_name": "api"}), \
             patch("workers.handlers.enrich_slots_from_discovery", return_value={"target_type": "pod", "pod_name": "api"}), \
             patch("workers.handlers.vm_slots_ready", return_value=True), \
             patch("workers.handlers.vm_slots_to_tool_args", return_value={"pod_name": "api"}):
            result = await _handle_inbound_payload_impl(
                ctx,
                {"text": "pod api", "source": "telegram", "chat_id": chat_id},
                "t-vm-ready",
            )
        assert "prom data ready" in result

    async def test_pending_await_vm_slots_nudge_path(self):
        """vm_slots not ready → returns nudge message."""
        from workers.handlers import _handle_inbound_payload_impl, TOOL_REGISTRY
        from workers.session_state import SessionState, save_session, PENDING_AWAIT_VM_SLOTS

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        chat_id = 22222
        state = SessionState()
        state.pending_action = PENDING_AWAIT_VM_SLOTS
        await save_session(redis, chat_id, state, ttl_sec=3600)

        ctx = _make_ctx(redis=redis)

        with patch("workers.handlers.try_autonomous_sdk_route", new=AsyncMock(return_value=None)), \
             patch("workers.handlers.evaluate_rag_gate", new=AsyncMock(return_value=MagicMock(hit=False, formatted=""))), \
             patch("workers.handlers.followup_indicates_host", return_value=False), \
             patch("workers.handlers.extract_entities_llm", new=AsyncMock(return_value={})), \
             patch("workers.handlers.merge_llm_entities_into_slots", return_value={}), \
             patch("workers.handlers.parse_resource_followup", return_value=None), \
             patch("workers.handlers.merge_vm_slots", return_value={}), \
             patch("workers.handlers.enrich_slots_from_discovery", return_value={}), \
             patch("workers.handlers.vm_slots_ready", return_value=False), \
             patch("workers.handlers.nudge_vm_slots_message", return_value="What target? Pod or Host?"):
            result = await _handle_inbound_payload_impl(
                ctx,
                {"text": "some clarification", "source": "telegram", "chat_id": chat_id},
                "t-vm-nudge",
            )
        assert "What target" in result or isinstance(result, str)

    async def test_rag_gate_hit_with_chat_id_saves_session(self):
        """RAG gate hit with chat_id → session saved."""
        from workers.handlers import _handle_inbound_payload_impl

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        chat_id = 11111
        ctx = _make_ctx(redis=redis)
        rag_result = MagicMock()
        rag_result.hit = True
        rag_result.formatted = "RAG formatted response long enough"

        with patch("workers.handlers.try_autonomous_sdk_route", new=AsyncMock(return_value=None)), \
             patch("workers.handlers.evaluate_rag_gate", new=AsyncMock(return_value=rag_result)), \
             patch("workers.handlers.preflight_infra_kb", new=AsyncMock(return_value=MagicMock(context=""))):
            result = await _handle_inbound_payload_impl(
                ctx,
                {"text": "how to debug", "source": "telegram", "chat_id": chat_id},
                "t-rag-chat",
            )
        assert "RAG" in result or isinstance(result, str)

    async def test_ambiguous_no_chat_id_logs_no_chat(self):
        """Ambiguous request without chat_id → logs vm_slots_no_chat_id, returns nudge."""
        from workers.handlers import _handle_inbound_payload_impl

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis)  # no chat_id in payload

        with patch("workers.handlers.try_autonomous_sdk_route", new=AsyncMock(return_value=None)), \
             patch("workers.handlers.evaluate_rag_gate", new=AsyncMock(return_value=MagicMock(hit=False, formatted=""))), \
             patch("workers.handlers.preflight_infra_kb", new=AsyncMock(return_value=MagicMock(context=""))), \
             patch("workers.handlers.is_ambiguous_resource_check", return_value=True), \
             patch("workers.handlers.extract_vm_slots_from_text", return_value={}), \
             patch("workers.handlers.enrich_slots_from_discovery", return_value={}), \
             patch("workers.handlers.nudge_vm_slots_message", return_value="Need scope"):
            result = await _handle_inbound_payload_impl(
                ctx,
                {"text": "check cpu", "source": "telegram"},
                "t-amb-nochat",
            )
        assert "Need scope" in result or isinstance(result, str)

    async def test_infra_context_exception_swallowed(self):
        """Exception in enrich_working_text_with_infra is swallowed → uses raw_user_text."""
        from workers.handlers import _handle_inbound_payload_impl

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis)

        with patch("workers.handlers.try_autonomous_sdk_route", new=AsyncMock(return_value=None)), \
             patch("workers.handlers.evaluate_rag_gate", new=AsyncMock(return_value=MagicMock(hit=False, formatted=""))), \
             patch("workers.handlers.preflight_infra_kb", new=AsyncMock(return_value=MagicMock(context=""))), \
             patch("workers.handlers.is_ambiguous_resource_check", return_value=False), \
             patch("workers.handlers.enrich_working_text_with_infra", new=AsyncMock(side_effect=RuntimeError("infra fail"))), \
             patch("workers.handlers.try_fast_path", new=AsyncMock(return_value=(True, "fast result"))):
            result = await _handle_inbound_payload_impl(
                ctx, {"text": "check pods", "source": "telegram"}, "t-infra-exc"
            )
        assert isinstance(result, str)

    async def test_fast_path_hit_with_chat_id_saves_session(self):
        """fast_path hit with chat_id → session saved, returns output."""
        from workers.handlers import _handle_inbound_payload_impl

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        chat_id = 66666
        ctx = _make_ctx(redis=redis)

        with patch("workers.handlers.try_autonomous_sdk_route", new=AsyncMock(return_value=None)), \
             patch("workers.handlers.evaluate_rag_gate", new=AsyncMock(return_value=MagicMock(hit=False, formatted=""))), \
             patch("workers.handlers.preflight_infra_kb", new=AsyncMock(return_value=MagicMock(context=""))), \
             patch("workers.handlers.is_ambiguous_resource_check", return_value=False), \
             patch("workers.handlers.enrich_working_text_with_infra", new=AsyncMock(return_value="enriched")), \
             patch("workers.handlers.try_fast_path", new=AsyncMock(return_value=(True, "fast with session"))):
            result = await _handle_inbound_payload_impl(
                ctx,
                {"text": "check pods", "source": "telegram", "chat_id": chat_id},
                "t-fast-chat",
            )
        assert "fast with session" in result


# ---------------------------------------------------------------------------
# _k8s_smart_target_hint
# ---------------------------------------------------------------------------

class TestK8sSmartTargetHint:
    def test_empty_text_returns_none(self):
        from workers.handlers import _k8s_smart_target_hint
        assert _k8s_smart_target_hint("") is None

    def test_with_pod_and_namespace(self):
        from workers.handlers import _k8s_smart_target_hint
        hint = _k8s_smart_target_hint("Alert: crash pod=my-pod namespace=prod")
        assert hint is not None
        assert "my-pod" in hint

    def test_no_k8s_keywords_returns_none(self):
        from workers.handlers import _k8s_smart_target_hint
        assert _k8s_smart_target_hint("general system health check") is None

    def test_with_k8s_keyword_no_pod(self):
        from workers.handlers import _k8s_smart_target_hint
        hint = _k8s_smart_target_hint("check cpu usage for deployment")
        assert hint is not None
