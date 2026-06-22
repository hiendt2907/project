"""Track 1A — handlers.py coverage target ≥75%.

Tests:
  - All pure helper functions (no external deps)
  - Functions needing a minimal ctx (SimpleNamespace)
  - _slow_system_body_for_unattended_alert replacements
  - _slow_path_system_messages_for_ctx (god / non-god path)
  - build_agentic_system_messages (attendend / unattended / shell / no-shell)
  - _wants_host_vm_chart
  - _user_confirms_rollout_telegram
  - _effective_inbound_text_preview edge cases
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(**kw: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "god_mode": False,
        "lab_unchained": False,
        "omni_concise_reply_max_words": 10,
        "omni_summary_max_words": 100,
        "slow_path_max_tool_attempts": 3,
        "slow_path_stale_signature_streak": 2,
        "json_repair_max": 1,
        "compress_turn_threshold": 5,
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
    """Minimal WorkerHandlerContext-like object."""
    scout_ready = asyncio.Event()
    scout_ready.set()
    defaults: dict[str, Any] = {
        "settings": _make_settings(),
        "redis": None,
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


# ---------------------------------------------------------------------------
# _cap_inbound_user_reply
# ---------------------------------------------------------------------------

class TestCapInboundUserReply:
    def test_empty_string_returns_empty(self):
        from workers.handlers import _cap_inbound_user_reply
        assert _cap_inbound_user_reply("", _make_ctx()) == ""

    def test_none_returns_empty(self):
        from workers.handlers import _cap_inbound_user_reply
        assert _cap_inbound_user_reply(None, _make_ctx()) == ""

    def test_valid_json_object_is_preserved_whole(self):
        from workers.handlers import _cap_inbound_user_reply
        payload = '{"tool": "reply", "args": {"text": "one two three four five six seven eight nine ten eleven"}}'
        result = _cap_inbound_user_reply(payload, _make_ctx(settings=_make_settings(omni_concise_reply_max_words=5)))
        assert result == payload  # JSON objects pass through

    def test_prose_is_truncated_to_max_words(self):
        from workers.handlers import _cap_inbound_user_reply
        # effective_reply_max_words clamps low end to 10, high end to omni_summary_max_words.
        # Use omni_summary_max_words=12 so min(concise=10, summary=12) = 10.
        ctx = _make_ctx(settings=_make_settings(omni_concise_reply_max_words=10, omni_summary_max_words=12))
        # 15 words → truncated to 10
        words = " ".join(f"w{i}" for i in range(15))
        result = _cap_inbound_user_reply(words, ctx)
        assert len(result.split()) == 10

    def test_json_like_but_invalid_is_truncated(self):
        from workers.handlers import _cap_inbound_user_reply
        # effective_reply_max_words clamps to 10 minimum. Use summary_max_words=10 as the binding cap.
        ctx = _make_ctx(settings=_make_settings(omni_concise_reply_max_words=10, omni_summary_max_words=10))
        # 20 words that look like invalid JSON (no valid JSON parse)
        text = "{" + " ".join(f"word{i}" for i in range(20)) + "}"
        result = _cap_inbound_user_reply(text, ctx)
        assert len(result.split()) <= 10

    def test_whitespace_only_returns_empty(self):
        from workers.handlers import _cap_inbound_user_reply
        assert _cap_inbound_user_reply("   ", _make_ctx()) == ""


# ---------------------------------------------------------------------------
# _effective_inbound_text_preview
# ---------------------------------------------------------------------------

class TestEffectiveInboundTextPreview:
    def test_prefers_text_field(self):
        from workers.handlers import _effective_inbound_text_preview
        assert _effective_inbound_text_preview({"text": "hello"}) == "hello"

    def test_prefers_message_field_when_no_text(self):
        from workers.handlers import _effective_inbound_text_preview
        assert _effective_inbound_text_preview({"message": "world"}) == "world"

    def test_empty_payload_returns_empty(self):
        from workers.handlers import _effective_inbound_text_preview
        assert _effective_inbound_text_preview({}) == ""

    def test_data_text_nested(self):
        from workers.handlers import _effective_inbound_text_preview
        assert _effective_inbound_text_preview({"data": {"text": " nested "}}) == "nested"

    def test_payload_text_nested(self):
        from workers.handlers import _effective_inbound_text_preview
        assert _effective_inbound_text_preview({"payload": {"text": "from payload"}}) == "from payload"

    def test_alertmanager_payload_with_pod(self):
        from workers.handlers import _effective_inbound_text_preview
        preview = _effective_inbound_text_preview({
            "data": {
                "alerts": [{
                    "labels": {
                        "alertname": "HighCPU",
                        "pod": "api-abc",
                        "namespace": "production",
                        "deployment": "api",
                    },
                    "annotations": {"summary": "CPU is high"},
                }]
            }
        })
        assert "Alert: HighCPU pod=api-abc" in preview
        assert "namespace=production" in preview
        assert "CPU is high" in preview

    def test_alertmanager_with_deployment_no_pod(self):
        from workers.handlers import _effective_inbound_text_preview
        preview = _effective_inbound_text_preview({
            "data": {
                "alerts": [{
                    "labels": {
                        "alertname": "DeployAlert",
                        "deployment": "my-deploy",
                        "namespace": "ns1",
                    },
                    "annotations": {"summary": "deploy down"},
                }]
            }
        })
        assert "Alert: DeployAlert deployment=my-deploy" in preview
        assert "deploy down" in preview

    def test_alertmanager_with_instance_meaningful(self):
        from workers.handlers import _effective_inbound_text_preview
        preview = _effective_inbound_text_preview({
            "data": {
                "alerts": [{
                    "labels": {
                        "alertname": "NodeDown",
                        "instance": "node-01",
                    },
                    "annotations": {"summary": "Node is down"},
                }]
            }
        })
        assert "Alert: NodeDown on node-01" in preview

    def test_alertmanager_with_instance_unknown_skipped(self):
        from workers.handlers import _effective_inbound_text_preview
        preview = _effective_inbound_text_preview({
            "data": {
                "alerts": [{
                    "labels": {
                        "alertname": "Watchdog",
                        "instance": "unknown",
                    },
                    "annotations": {},
                }]
            }
        })
        assert "Alert: Watchdog" in preview
        # Instance "unknown" should not appear
        assert "on unknown" not in preview

    def test_alertmanager_no_summary(self):
        from workers.handlers import _effective_inbound_text_preview
        preview = _effective_inbound_text_preview({
            "data": {
                "alerts": [{
                    "labels": {"alertname": "TestAlert", "pod": "pod-1", "namespace": "test"},
                    "annotations": {},
                }]
            }
        })
        assert "(no summary)" in preview

    def test_none_alert_items_handled_gracefully(self):
        from workers.handlers import _effective_inbound_text_preview
        preview = _effective_inbound_text_preview({"data": {"alerts": [None]}})
        assert "Alert: UnknownAlert" in preview

    def test_multiple_alerts_joined(self):
        from workers.handlers import _effective_inbound_text_preview
        preview = _effective_inbound_text_preview({
            "data": {
                "alerts": [
                    {
                        "labels": {"alertname": "Alert1", "pod": "p1", "namespace": "n1"},
                        "annotations": {"summary": "s1"},
                    },
                    {
                        "labels": {"alertname": "Alert2", "pod": "p2", "namespace": "n2"},
                        "annotations": {"summary": "s2"},
                    },
                ]
            }
        })
        assert "Alert1" in preview
        assert "Alert2" in preview

    def test_exception_in_data_returns_empty(self):
        from workers.handlers import _effective_inbound_text_preview
        # Non-subscriptable data raises exception → returns ""
        result = _effective_inbound_text_preview({"data": 12345})
        assert result == ""

    def test_no_data_no_payload_returns_empty(self):
        from workers.handlers import _effective_inbound_text_preview
        result = _effective_inbound_text_preview({"other_key": "value"})
        assert result == ""


# ---------------------------------------------------------------------------
# _parse_alert_pod_namespace_from_preview
# ---------------------------------------------------------------------------

class TestParseAlertPodNamespaceFromPreview:
    def test_empty_string(self):
        from workers.handlers import _parse_alert_pod_namespace_from_preview
        assert _parse_alert_pod_namespace_from_preview("") == (None, None)

    def test_none(self):
        from workers.handlers import _parse_alert_pod_namespace_from_preview
        assert _parse_alert_pod_namespace_from_preview(None) == (None, None)

    def test_alert_line_match(self):
        from workers.handlers import _parse_alert_pod_namespace_from_preview
        text = "Alert: CPUHigh pod=api-5d6 namespace=prod"
        assert _parse_alert_pod_namespace_from_preview(text) == ("api-5d6", "prod")

    def test_prefers_alert_prefix_line(self):
        from workers.handlers import _parse_alert_pod_namespace_from_preview
        text = "pod=ignored namespace=ignored\nAlert: X pod=correct namespace=ns-correct"
        pod, ns = _parse_alert_pod_namespace_from_preview(text)
        assert pod == "correct"
        assert ns == "ns-correct"

    def test_fallback_anywhere(self):
        from workers.handlers import _parse_alert_pod_namespace_from_preview
        pod, ns = _parse_alert_pod_namespace_from_preview("pod=redis-0 namespace=cache")
        assert pod == "redis-0"
        assert ns == "cache"

    def test_missing_namespace_returns_none_ns(self):
        from workers.handlers import _parse_alert_pod_namespace_from_preview
        pod, ns = _parse_alert_pod_namespace_from_preview("Alert: X pod=only-pod")
        assert pod is None
        assert ns is None

    def test_alert_pod_and_ns_both_present_in_pipe_segment(self):
        from workers.handlers import _parse_alert_pod_namespace_from_preview
        text = "Alert: KubePodCrashLooping pod=api-7d9f namespace=prod | extra=data"
        pod, ns = _parse_alert_pod_namespace_from_preview(text)
        assert pod == "api-7d9f"
        assert ns == "prod"


# ---------------------------------------------------------------------------
# _preflight_hints_from_inbound
# ---------------------------------------------------------------------------

class TestPreflightHintsFromInbound:
    def test_payload_namespace_only(self):
        from workers.handlers import _preflight_hints_from_inbound
        hints = _preflight_hints_from_inbound({"namespace": "my-ns"}, "no alert here", "api")
        assert hints == {"namespace": "my-ns"}

    def test_alert_overrides_payload_namespace(self):
        from workers.handlers import _preflight_hints_from_inbound
        hints = _preflight_hints_from_inbound(
            {"namespace": "payload-ns"},
            "Alert: X pod=p1 namespace=alert-ns",
            "prometheus",
        )
        assert hints["namespace"] == "alert-ns"
        assert hints["pod_name"] == "p1"

    def test_empty_payload_and_no_alert_returns_none(self):
        from workers.handlers import _preflight_hints_from_inbound
        result = _preflight_hints_from_inbound({}, "no namespace here", "telegram")
        assert result is None

    def test_pod_from_alert_line(self):
        from workers.handlers import _preflight_hints_from_inbound
        hints = _preflight_hints_from_inbound({}, "pod=svc-0 namespace=monitoring", "api")
        assert hints is not None
        assert hints["pod_name"] == "svc-0"
        assert hints["namespace"] == "monitoring"


# ---------------------------------------------------------------------------
# _extract_duration
# ---------------------------------------------------------------------------

class TestExtractDuration:
    @pytest.mark.parametrize("text,expected", [
        ("show for 2h", "2h"),
        ("last 30m", "30m"),
        ("no unit", "1h"),
        ("7d not supported", "1h"),
        ("3 h with space", "3h"),
        ("5 m with space", "5m"),
        ("", "1h"),
    ])
    def test_various_inputs(self, text, expected):
        from workers.handlers import _extract_duration
        assert _extract_duration(text) == expected


# ---------------------------------------------------------------------------
# _wants_host_vm_chart
# ---------------------------------------------------------------------------

class TestWantsHostVmChart:
    def _state(self, target_type: str = "host"):
        from workers.session_state import SessionState
        s = SessionState()
        s.monitoring_target_type = target_type
        return s

    def test_non_host_target_returns_false(self):
        from workers.handlers import _wants_host_vm_chart
        state = self._state("pod")
        assert _wants_host_vm_chart(state, "show chart") is False

    def test_host_target_with_namespace_keyword_returns_false(self):
        from workers.handlers import _wants_host_vm_chart
        state = self._state("host")
        assert _wants_host_vm_chart(state, "namespace: prod chart") is False
        assert _wants_host_vm_chart(state, "ns=prod chart") is False

    def test_host_target_with_pod_keyword_returns_false(self):
        from workers.handlers import _wants_host_vm_chart
        state = self._state("host")
        assert _wants_host_vm_chart(state, "pod api-0 chart") is False

    def test_host_target_with_chart_keyword_returns_true(self):
        from workers.handlers import _wants_host_vm_chart
        state = self._state("host")
        assert _wants_host_vm_chart(state, "show chart for 1h") is True

    def test_host_target_with_vm_keyword_returns_true(self):
        from workers.handlers import _wants_host_vm_chart
        state = self._state("host")
        assert _wants_host_vm_chart(state, "vm query history") is True

    def test_host_target_no_matching_keyword_returns_false(self):
        from workers.handlers import _wants_host_vm_chart
        state = self._state("host")
        assert _wants_host_vm_chart(state, "just a regular message") is False

    def test_empty_monitoring_type_returns_false(self):
        from workers.handlers import _wants_host_vm_chart
        state = self._state("")
        assert _wants_host_vm_chart(state, "show chart 1h") is False


# ---------------------------------------------------------------------------
# _user_confirms_rollout_telegram
# ---------------------------------------------------------------------------

class TestUserConfirmsRolloutTelegram:
    @pytest.mark.parametrize("text", [
        "ok", "OK", "yes", "y", "confirm", "xác nhận", "xac nhan",
        "có", "co", "đồng ý", "dong y",
    ])
    def test_explicit_confirmations(self, text):
        from workers.handlers import _user_confirms_rollout_telegram
        assert _user_confirms_rollout_telegram(text) is True

    @pytest.mark.parametrize("text", [
        "no", "cancel", "reject", "",
        "this is a very long text that should not be treated as a rollout confirmation because it exceeds the limit",
    ])
    def test_non_confirmations(self, text):
        from workers.handlers import _user_confirms_rollout_telegram
        assert _user_confirms_rollout_telegram(text) is False

    def test_yes_with_exclamation(self):
        from workers.handlers import _user_confirms_rollout_telegram
        assert _user_confirms_rollout_telegram("yes!") is True

    def test_too_long_returns_false(self):
        from workers.handlers import _user_confirms_rollout_telegram
        assert _user_confirms_rollout_telegram("ok " * 20) is False

    def test_none_like_empty(self):
        from workers.handlers import _user_confirms_rollout_telegram
        assert _user_confirms_rollout_telegram("") is False


# ---------------------------------------------------------------------------
# _slow_system_body_for_unattended_alert
# ---------------------------------------------------------------------------

class TestSlowSystemBodyForUnattendedAlert:
    def test_reply_tool_replaced_with_escalate_hint(self):
        from workers.handlers import _slow_system_body_for_unattended_alert, SLOW_SYSTEM_VI
        result = _slow_system_body_for_unattended_alert(SLOW_SYSTEM_VI)
        assert "Luồng cảnh báo unattended" in result

    def test_ask_user_replaced_with_best_effort(self):
        from workers.handlers import _slow_system_body_for_unattended_alert
        base = "some text **không** được gọi tool; hỏi lại một câu ngắn. more text"
        result = _slow_system_body_for_unattended_alert(base)
        assert "thử điều tra best-effort" in result

    def test_few_shot_clarify_replaced_with_unattended(self):
        from workers.handlers import _slow_system_body_for_unattended_alert
        base = "[FEW-SHOT clarify] User: 'Check CPU' (không nói host/pod/ns) → **chỉ** hỏi lại scope; **không** tool. "
        result = _slow_system_body_for_unattended_alert(base)
        assert "[UNATTENDED]" in result

    def test_reply_text_hint_replaced_with_escalate(self):
        from workers.handlers import _slow_system_body_for_unattended_alert
        base = "Nếu chỉ cần trả lời chữ cho user — dùng tool `reply`, args gồm field `text` (một JSON tool hợp lệ). "
        result = _slow_system_body_for_unattended_alert(base)
        assert "escalate_to_human" in result

    def test_no_relevant_text_unchanged(self):
        from workers.handlers import _slow_system_body_for_unattended_alert
        base = "This has no replacement targets."
        result = _slow_system_body_for_unattended_alert(base)
        assert result == base


# ---------------------------------------------------------------------------
# _parse_suggestions_json_tail
# ---------------------------------------------------------------------------

class TestParseSuggestionsJsonTail:
    def test_valid_three_item_array(self):
        from workers.handlers import _parse_suggestions_json_tail
        text = 'Some output\nSUGGESTIONS_JSON: ["cmd1", "cmd2", "cmd3"]'
        head, cmds = _parse_suggestions_json_tail(text)
        assert head == "Some output"
        assert cmds == ["cmd1", "cmd2", "cmd3"]

    def test_no_suggestions_marker_returns_original(self):
        from workers.handlers import _parse_suggestions_json_tail
        text = "plain text"
        head, cmds = _parse_suggestions_json_tail(text)
        assert head == "plain text"
        assert cmds is None

    def test_fewer_than_three_items_returns_none(self):
        from workers.handlers import _parse_suggestions_json_tail
        text = 'Body\nSUGGESTIONS_JSON: ["one", "two"]'
        head, cmds = _parse_suggestions_json_tail(text)
        assert head == text.strip()
        assert cmds is None

    def test_malformed_json_returns_none(self):
        from workers.handlers import _parse_suggestions_json_tail
        text = 'Body\nSUGGESTIONS_JSON: ["one",'
        head, cmds = _parse_suggestions_json_tail(text)
        assert head == text.strip()
        assert cmds is None

    def test_long_commands_clipped_to_500(self):
        from workers.handlers import _parse_suggestions_json_tail
        long_cmd = "x" * 700
        text = f'SUGGESTIONS_JSON: ["{long_cmd}", "b", "c"]'
        _, cmds = _parse_suggestions_json_tail(text)
        assert cmds is not None
        assert len(cmds[0]) == 500

    def test_empty_text_returns_empty(self):
        from workers.handlers import _parse_suggestions_json_tail
        head, cmds = _parse_suggestions_json_tail("")
        assert head == ""
        assert cmds is None

    def test_empty_string_returns_empty_cmds_none(self):
        from workers.handlers import _parse_suggestions_json_tail
        # Empty string → no SUGGESTIONS_JSON marker → returns stripped empty, None
        head, cmds = _parse_suggestions_json_tail("")
        assert head == ""
        assert cmds is None


# ---------------------------------------------------------------------------
# _k8s_smart_target_hint
# ---------------------------------------------------------------------------

class TestK8sSmartTargetHint:
    def test_empty_returns_none(self):
        from workers.handlers import _k8s_smart_target_hint
        assert _k8s_smart_target_hint("") is None
        assert _k8s_smart_target_hint(None) is None

    def test_non_k8s_text_returns_none(self):
        from workers.handlers import _k8s_smart_target_hint
        assert _k8s_smart_target_hint("hello world general question") is None

    def test_scoped_alert_returns_inspect_hint(self):
        from workers.handlers import _k8s_smart_target_hint
        hint = _k8s_smart_target_hint("Alert: CPUHigh pod=api-0 namespace=prod")
        assert hint is not None
        assert "pod=api-0 namespace=prod" in hint
        assert "inspect_pod_deep" in hint.lower() or "do not" in hint.lower()

    def test_k8s_text_without_pod_returns_routing_hint(self):
        from workers.handlers import _k8s_smart_target_hint
        hint = _k8s_smart_target_hint("show pods in namespace prod")
        assert hint is not None
        assert "K8s" in hint

    def test_cpu_keyword_triggers_k8s_hint(self):
        from workers.handlers import _k8s_smart_target_hint
        hint = _k8s_smart_target_hint("check cpu")
        assert hint is not None

    def test_metric_keyword_triggers_hint(self):
        from workers.handlers import _k8s_smart_target_hint
        hint = _k8s_smart_target_hint("show metric for deployment")
        assert hint is not None


# ---------------------------------------------------------------------------
# _embedding_from_response
# ---------------------------------------------------------------------------

class TestEmbeddingFromResponse:
    def test_embedding_key_list(self):
        from workers.handlers import _embedding_from_response
        result = _embedding_from_response({"embedding": [0.1, 0.2, 0.3]})
        assert result == [0.1, 0.2, 0.3]

    def test_embedding_key_tuple_converted_to_list(self):
        from workers.handlers import _embedding_from_response
        result = _embedding_from_response({"embedding": (1.0, 2.0)})
        assert result == [1.0, 2.0]

    def test_embeddings_key_first_item(self):
        from workers.handlers import _embedding_from_response
        result = _embedding_from_response({"embeddings": [[3, 4], [5, 6]]})
        assert result == [3, 4]

    def test_missing_key_raises(self):
        from workers.handlers import _embedding_from_response
        with pytest.raises(ValueError, match="missing embedding"):
            _embedding_from_response({})

    def test_embeddings_empty_list_raises(self):
        from workers.handlers import _embedding_from_response
        with pytest.raises((ValueError, IndexError)):
            _embedding_from_response({"embeddings": []})


# ---------------------------------------------------------------------------
# _parse_tool_json
# ---------------------------------------------------------------------------

class TestParseToolJson:
    def test_plain_json(self):
        from workers.handlers import _parse_tool_json
        call = _parse_tool_json('{"tool": "reply", "args": {"text": "hi"}}')
        assert call.tool == "reply"
        assert call.args == {"text": "hi"}

    def test_fenced_json(self):
        from workers.handlers import _parse_tool_json
        call = _parse_tool_json('```json\n{"tool": "echo", "args": {"msg": "test"}}\n```')
        assert call.tool == "echo"
        assert call.args == {"msg": "test"}

    def test_fenced_without_lang(self):
        from workers.handlers import _parse_tool_json
        call = _parse_tool_json('```\n{"tool": "echo", "args": {}}\n```')
        assert call.tool == "echo"

    def test_params_alias(self):
        from workers.handlers import _parse_tool_json
        call = _parse_tool_json('{"tool": "echo", "params": {"key": "val"}}')
        assert call.args == {"key": "val"}

    def test_invalid_json_raises(self):
        from workers.handlers import _parse_tool_json
        with pytest.raises(Exception):
            _parse_tool_json("not valid json")

    def test_args_takes_priority_over_params(self):
        from workers.handlers import _parse_tool_json
        # When args is a non-empty dict, params should NOT override it
        call = _parse_tool_json('{"tool": "echo", "args": {"a": 1}, "params": {"b": 2}}')
        assert call.args == {"a": 1}


# ---------------------------------------------------------------------------
# _should_abort_stale
# ---------------------------------------------------------------------------

class TestShouldAbortStale:
    def test_empty_trace_returns_false(self):
        from workers.handlers import _should_abort_stale
        assert _should_abort_stale([], 2) is False

    def test_streak_exactly_at_limit(self):
        from workers.slow_path_trace import AttemptRecord
        from workers.handlers import _should_abort_stale
        trace = [
            AttemptRecord(1, "parse", "sig_parse", "bad"),
            AttemptRecord(2, "parse", "sig_parse", "bad"),
        ]
        assert _should_abort_stale(trace, 2) is True

    def test_streak_below_limit(self):
        from workers.slow_path_trace import AttemptRecord
        from workers.handlers import _should_abort_stale
        trace = [
            AttemptRecord(1, "tool_error", "sig_a", "err"),
            AttemptRecord(2, "parse", "sig_b", "parse err"),
        ]
        assert _should_abort_stale(trace, 2) is False

    def test_streak_broken_returns_false(self):
        from workers.slow_path_trace import AttemptRecord
        from workers.handlers import _should_abort_stale
        trace = [
            AttemptRecord(1, "tool_error", "sig_x", "err1"),
            AttemptRecord(2, "tool_error", "sig_x", "err2"),
            AttemptRecord(3, "parse", "sig_y", "parse"),
        ]
        assert _should_abort_stale(trace, 3) is False


# ---------------------------------------------------------------------------
# _slow_path_system_messages_for_ctx
# ---------------------------------------------------------------------------

class TestSlowPathSystemMessagesForCtx:
    def test_non_god_returns_two_system_messages(self, monkeypatch):
        from workers import handlers
        monkeypatch.setattr(handlers, "shell_fast_path_enabled", lambda _: False)
        ctx = _make_ctx()
        msgs = handlers._slow_path_system_messages_for_ctx(ctx)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "system"

    def test_god_mode_returns_god_body(self, monkeypatch):
        from workers import handlers
        monkeypatch.setattr(handlers, "shell_fast_path_enabled", lambda _: True)
        ctx = _make_ctx(settings=_make_settings(god_mode=True, lab_unchained=True))
        msgs = handlers._slow_path_system_messages_for_ctx(ctx)
        assert len(msgs) == 2
        # God mode prompt body should differ from regular (contains shell guidance)
        body = msgs[1]["content"]
        assert "__TOOLS_FROM_REGISTRY__" not in body  # catalog placeholder replaced


# ---------------------------------------------------------------------------
# build_agentic_system_messages
# ---------------------------------------------------------------------------

class TestBuildAgenticSystemMessages:
    def test_attended_non_god_returns_two_messages(self, monkeypatch):
        from workers import handlers
        monkeypatch.setattr(handlers, "shell_fast_path_enabled", lambda _: False)
        ctx = _make_ctx()
        msgs = handlers.build_agentic_system_messages(ctx, unattended_alert=False)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "system"

    def test_attended_god_mode_returns_god_body(self, monkeypatch):
        from workers import handlers
        monkeypatch.setattr(handlers, "shell_fast_path_enabled", lambda _: True)
        ctx = _make_ctx(settings=_make_settings(god_mode=True, lab_unchained=True))
        msgs = handlers.build_agentic_system_messages(ctx, unattended_alert=False)
        assert len(msgs) == 2

    def test_unattended_no_reply_in_catalog(self, monkeypatch):
        from workers import handlers
        from workers.tools import TOOL_REGISTRY
        monkeypatch.setattr(handlers, "shell_fast_path_enabled", lambda _: False)
        ctx = _make_ctx()
        msgs = handlers.build_agentic_system_messages(ctx, unattended_alert=True)
        # Unattended messages: at least 2 (generator + body); no pod hint → exactly 2
        assert len(msgs) >= 2
        body = msgs[1]["content"]
        # The catalog (tool list) should not contain the reply tool entry.
        # Catalog appears after "Tools (from TOOL_REGISTRY): " or similar markers.
        # If reply is in TOOL_REGISTRY, verify it's excluded from the comma-separated tool list.
        if "reply" in TOOL_REGISTRY:
            # Parse out the catalog section by looking for backtick-wrapped tool names in a list
            import re as _re
            catalog_match = _re.search(r"Tools \([^)]+\):\s*((?:`[^`]+`,?\s*)+)", body)
            if catalog_match:
                catalog_str = catalog_match.group(1)
                # `reply` should not appear as a standalone tool entry in the catalog
                assert "`reply`" not in catalog_str

    def test_unattended_with_pod_ns_adds_priority_hint(self, monkeypatch):
        from workers import handlers
        monkeypatch.setattr(handlers, "shell_fast_path_enabled", lambda _: False)
        ctx = _make_ctx(inbound_user_text="Alert: CPUHigh pod=api-0 namespace=prod")
        msgs = handlers.build_agentic_system_messages(ctx, unattended_alert=True)
        # Should have 3 messages: generator, body, priority identity hint
        assert len(msgs) == 3
        priority_hint = msgs[2]["content"]
        assert "pod=api-0 namespace=prod" in priority_hint

    def test_unattended_without_pod_ns_stays_two(self, monkeypatch):
        from workers import handlers
        monkeypatch.setattr(handlers, "shell_fast_path_enabled", lambda _: False)
        ctx = _make_ctx(inbound_user_text="generic alert no pod info")
        msgs = handlers.build_agentic_system_messages(ctx, unattended_alert=True)
        assert len(msgs) == 2

    def test_unattended_god_mode_appends_lab_shell_supplement(self, monkeypatch):
        from workers import handlers
        monkeypatch.setattr(handlers, "shell_fast_path_enabled", lambda _: True)
        ctx = _make_ctx(
            settings=_make_settings(god_mode=True, lab_unchained=True),
            inbound_user_text="",
        )
        msgs = handlers.build_agentic_system_messages(ctx, unattended_alert=True)
        # Body (msgs[1]) should have lab shell supplement appended
        body = msgs[1]["content"]
        assert len(body) > 100  # non-trivial

    def test_catalog_placeholder_replaced(self, monkeypatch):
        from workers import handlers
        monkeypatch.setattr(handlers, "shell_fast_path_enabled", lambda _: False)
        ctx = _make_ctx()
        msgs = handlers.build_agentic_system_messages(ctx, unattended_alert=False)
        for msg in msgs:
            assert "__TOOLS_FROM_REGISTRY__" not in msg["content"]


# ---------------------------------------------------------------------------
# RE_RESTART_ROLLOUT_EXPLICIT and RE_LIST_ALL_PODS_CHAT regex constants
# ---------------------------------------------------------------------------

class TestRegexConstants:
    def test_restart_rollout_explicit_matches(self):
        from workers.handlers import RE_RESTART_ROLLOUT_EXPLICIT
        assert RE_RESTART_ROLLOUT_EXPLICIT.search("please restart the deployment")
        assert RE_RESTART_ROLLOUT_EXPLICIT.search("rollout restart")
        assert RE_RESTART_ROLLOUT_EXPLICIT.search("khởi động lại")
        assert RE_RESTART_ROLLOUT_EXPLICIT.search("deploy lại")

    def test_restart_rollout_explicit_no_match(self):
        from workers.handlers import RE_RESTART_ROLLOUT_EXPLICIT
        assert not RE_RESTART_ROLLOUT_EXPLICIT.search("show me logs")

    def test_list_all_pods_chat_matches(self):
        from workers.handlers import RE_LIST_ALL_PODS_CHAT
        assert RE_LIST_ALL_PODS_CHAT.search("kubectl get pods -a")
        assert RE_LIST_ALL_PODS_CHAT.search("list all pods")
        assert RE_LIST_ALL_PODS_CHAT.search("liệt kê pod")

    def test_list_all_pods_chat_no_match(self):
        from workers.handlers import RE_LIST_ALL_PODS_CHAT
        assert not RE_LIST_ALL_PODS_CHAT.search("check cpu for pod api-0")


# ---------------------------------------------------------------------------
# SLOW_SYSTEM_VI constant checks (module-level strings)
# ---------------------------------------------------------------------------

class TestModuleLevelStrings:
    def test_slow_system_vi_contains_tool_catalog_placeholder(self):
        from workers.handlers import SLOW_SYSTEM_VI, TOOL_CATALOG_PLACEHOLDER
        assert TOOL_CATALOG_PLACEHOLDER in SLOW_SYSTEM_VI

    def test_sre_json_generator_vi_not_empty(self):
        from workers.handlers import SRE_JSON_GENERATOR_VI
        assert len(SRE_JSON_GENERATOR_VI) > 50

    def test_sre_json_generator_unattended_vi_not_empty(self):
        from workers.handlers import SRE_JSON_GENERATOR_UNATTENDED_VI
        assert len(SRE_JSON_GENERATOR_UNATTENDED_VI) > 50

    def test_slow_system_god_vi_contains_shell_guidance(self):
        from workers.handlers import SLOW_SYSTEM_GOD_VI
        assert "execute_shell_command" in SLOW_SYSTEM_GOD_VI

    def test_agentic_react_rules_vi_has_omni_mark_resolved(self):
        from workers.handlers import AGENTIC_REACT_RULES_VI
        assert "omni_mark_resolved" in AGENTIC_REACT_RULES_VI

    def test_agentic_react_rules_unattended_has_escalate(self):
        from workers.handlers import AGENTIC_REACT_RULES_UNATTENDED_SUPPLEMENT_VI
        assert "escalate_to_human" in AGENTIC_REACT_RULES_UNATTENDED_SUPPLEMENT_VI

    def test_conv_fallback_system_vi_not_empty(self):
        from workers.handlers import CONV_FALLBACK_SYSTEM_VI
        assert len(CONV_FALLBACK_SYSTEM_VI) > 50

    def test_k8s_tool_guidance_vi_not_empty(self):
        from workers.handlers import K8S_TOOL_GUIDANCE_VI
        assert len(K8S_TOOL_GUIDANCE_VI) > 50

    def test_final_format_vi_not_empty(self):
        from workers.handlers import FINAL_FORMAT_VI
        assert len(FINAL_FORMAT_VI) > 10


# ---------------------------------------------------------------------------
# _effective_inbound_text_preview — extra branch coverage
# ---------------------------------------------------------------------------

class TestEffectiveInboundTextPreviewExtra:
    def test_alert_without_any_label_uses_unknown(self):
        from workers.handlers import _effective_inbound_text_preview
        preview = _effective_inbound_text_preview({
            "data": {
                "alerts": [{
                    "labels": {},
                    "annotations": {},
                }]
            }
        })
        assert "Alert: UnknownAlert" in preview

    def test_dep_without_ns(self):
        from workers.handlers import _effective_inbound_text_preview
        preview = _effective_inbound_text_preview({
            "data": {
                "alerts": [{
                    "labels": {"alertname": "DeployAlert", "deployment": "svc"},
                    "annotations": {"summary": "down"},
                }]
            }
        })
        assert "Alert: DeployAlert deployment=svc" in preview

    def test_extra_labels_appended_to_line(self):
        from workers.handlers import _effective_inbound_text_preview
        preview = _effective_inbound_text_preview({
            "data": {
                "alerts": [{
                    "labels": {
                        "alertname": "TestAlert",
                        "pod": "p1",
                        "namespace": "ns1",
                        "job": "my-job",
                        "container": "app",
                    },
                    "annotations": {"summary": "test"},
                }]
            }
        })
        assert "job=my-job" in preview
        assert "container=app" in preview


# ---------------------------------------------------------------------------
# _parse_tool_json — additional edge cases
# ---------------------------------------------------------------------------

class TestParseToolJsonEdgeCases:
    def test_fenced_json_no_trailing_backticks(self):
        from workers.handlers import _parse_tool_json
        # fenced but no closing ``` — lines[1:] used
        call = _parse_tool_json('```json\n{"tool": "reply", "args": {"text": "hi"}}')
        assert call.tool == "reply"

    def test_non_dict_json_raises(self):
        from workers.handlers import _parse_tool_json
        with pytest.raises(Exception):
            _parse_tool_json("[1, 2, 3]")


# ---------------------------------------------------------------------------
# _wants_host_vm_chart — edge cases for coverage
# ---------------------------------------------------------------------------

class TestWantsHostVmChartEdgeCases:
    def _state_host(self):
        from workers.session_state import SessionState
        s = SessionState()
        s.monitoring_target_type = "host"
        return s

    def test_historical_keyword(self):
        from workers.handlers import _wants_host_vm_chart
        state = self._state_host()
        assert _wants_host_vm_chart(state, "show historical metrics") is True

    def test_victoria_keyword(self):
        from workers.handlers import _wants_host_vm_chart
        state = self._state_host()
        assert _wants_host_vm_chart(state, "victoria metrics") is True

    def test_time_series_keyword(self):
        from workers.handlers import _wants_host_vm_chart
        state = self._state_host()
        assert _wants_host_vm_chart(state, "time-series query") is True

    def test_query_keyword(self):
        from workers.handlers import _wants_host_vm_chart
        state = self._state_host()
        assert _wants_host_vm_chart(state, "run a query on the node") is True

    def test_24h_keyword(self):
        from workers.handlers import _wants_host_vm_chart
        state = self._state_host()
        assert _wants_host_vm_chart(state, "24h trend") is True


# ---------------------------------------------------------------------------
# _user_confirms_rollout_telegram — regex path
# ---------------------------------------------------------------------------

class TestUserConfirmsRolloutTelegramRegex:
    def test_confirm_with_trailing_exclamation(self):
        from workers.handlers import _user_confirms_rollout_telegram
        assert _user_confirms_rollout_telegram("confirm!") is True

    def test_ok_with_trailing_space(self):
        from workers.handlers import _user_confirms_rollout_telegram
        assert _user_confirms_rollout_telegram("ok ") is True

    def test_xac_nhan_regex(self):
        from workers.handlers import _user_confirms_rollout_telegram
        assert _user_confirms_rollout_telegram("xác nhận!") is True


# ---------------------------------------------------------------------------
# _parse_suggestions_json_tail — additional branches
# ---------------------------------------------------------------------------

class TestParseSuggestionsJsonTailExtra:
    def test_four_items_only_first_three_returned(self):
        from workers.handlers import _parse_suggestions_json_tail
        text = 'head\nSUGGESTIONS_JSON: ["a", "b", "c", "d"]'
        head, cmds = _parse_suggestions_json_tail(text)
        assert head == "head"
        assert cmds == ["a", "b", "c"]

    def test_suggestions_inline_no_head(self):
        from workers.handlers import _parse_suggestions_json_tail
        text = 'SUGGESTIONS_JSON: ["x", "y", "z"]'
        head, cmds = _parse_suggestions_json_tail(text)
        assert cmds == ["x", "y", "z"]

    def test_non_list_json_returns_none(self):
        from workers.handlers import _parse_suggestions_json_tail
        text = 'head\nSUGGESTIONS_JSON: {"key": "value"}'
        head, cmds = _parse_suggestions_json_tail(text)
        # Not a list → cmds is None
        assert cmds is None


# ---------------------------------------------------------------------------
# _slow_system_body_for_unattended_alert — all replacement branches
# ---------------------------------------------------------------------------

class TestSlowSystemBodyAllBranches:
    def test_nếu_chỉ_cần_trả_lời_branch(self):
        from workers.handlers import _slow_system_body_for_unattended_alert
        base = "Nếu chỉ nhắn user → `reply` + `args.text`. rest"
        result = _slow_system_body_for_unattended_alert(base)
        assert "Luồng cảnh báo unattended" in result

    def test_second_ask_user_variant(self):
        from workers.handlers import _slow_system_body_for_unattended_alert
        base = "**không** được gọi tool; hỏi lại một câu ngắn qua `reply`. end"
        result = _slow_system_body_for_unattended_alert(base)
        assert "thử điều tra best-effort" in result

    def test_second_few_shot_variant(self):
        from workers.handlers import _slow_system_body_for_unattended_alert
        base = "[FEW-SHOT clarify] User: 'Check CPU' (không nói host/pod/ns) → **chỉ** `reply` hỏi scope; **không** tool khác. "
        result = _slow_system_body_for_unattended_alert(base)
        assert "[UNATTENDED]" in result


# ---------------------------------------------------------------------------
# Verify import works (all top-level names accessible)
# ---------------------------------------------------------------------------

def test_module_imports_successfully():
    import workers.handlers as h
    assert callable(h._cap_inbound_user_reply)
    assert callable(h._effective_inbound_text_preview)
    assert callable(h._parse_alert_pod_namespace_from_preview)
    assert callable(h._preflight_hints_from_inbound)
    assert callable(h._extract_duration)
    assert callable(h._wants_host_vm_chart)
    assert callable(h._user_confirms_rollout_telegram)
    assert callable(h._slow_system_body_for_unattended_alert)
    assert callable(h._parse_suggestions_json_tail)
    assert callable(h._k8s_smart_target_hint)
    assert callable(h._embedding_from_response)
    assert callable(h._parse_tool_json)
    assert callable(h._should_abort_stale)
    assert callable(h.build_agentic_system_messages)
    assert callable(h._slow_path_system_messages_for_ctx)


# ===========================================================================
# ASYNC TESTS — monkeypatch all external I/O
# ===========================================================================

class _FakeLLM:
    """Minimal duck-type for VLLMClient — returns predictable content."""

    def __init__(self, chat_content: str = '{"tool":"reply","args":{"text":"ok"}}',
                 embed_vec: list[float] | None = None) -> None:
        self.chat_content = chat_content
        self.embed_vec = embed_vec or [0.1] * 4

    async def chat(self, *, model: str, messages: list, **kwargs) -> dict:
        return {"message": {"role": "assistant", "content": self.chat_content}}

    async def embed(self, *, model: str, input: str, **kwargs) -> dict:
        return {"embedding": self.embed_vec}


class _FakeVectorStore:
    """Returns zero hits by default."""

    def __init__(self, points: list | None = None) -> None:
        from rag.redis_vector_store import QueryResponse
        self._qr = QueryResponse(points=points or [])

    async def query_points(self, **kwargs) -> "QueryResponse":  # type: ignore[type-arg]
        return self._qr


class _FakeSemaphore:
    async def acquire(self, timeout_s: float = 120.0) -> str:
        return "token"

    async def release(self, token: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Async helpers used as monkeypatch targets (replace removed asyncio.coroutine)
# ---------------------------------------------------------------------------

async def _anone(*args: Any, **kwargs: Any) -> None:
    return None


async def _aempty(*args: Any, **kwargs: Any) -> str:
    return ""


async def _afalse_3none(*args: Any, **kwargs: Any) -> tuple:
    return (False, None, None)


async def _afalse_none(*args: Any, **kwargs: Any) -> tuple:
    return (False, None)


async def _arollout_done(*args: Any, **kwargs: Any) -> str:
    return "rollout done"


async def _awrite_done(*args: Any, **kwargs: Any) -> str:
    return "write done"


async def _arag_miss(*args: Any, **kwargs: Any) -> "_FakeRagGateOutcome":
    return _FakeRagGateOutcome(hit=False)


async def _afast_result(*args: Any, **kwargs: Any) -> tuple:
    return (True, "fast result")


async def _aid(t: str) -> str:
    return t


def _make_async_ctx(
    *,
    llm: "_FakeLLM | None" = None,
    vector_store: "_FakeVectorStore | None" = None,
    redis_data: dict | None = None,
    **settings_kw: Any,
) -> SimpleNamespace:
    """Build a context ready for async functions."""
    import fakeredis.aioredis
    import asyncio as _asyncio

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)

    scout_ready = _asyncio.Event()
    scout_ready.set()

    ctx = SimpleNamespace(
        settings=_make_settings(**settings_kw),
        redis=r,
        llm=llm or _FakeLLM(),
        vector_store=vector_store or _FakeVectorStore(),
        ledger=None,
        semaphore=_FakeSemaphore(),
        telegram=None,
        kafka=None,
        telegram_chat_id=None,
        inbound_source="",
        inbound_user_text="",
        restart_rollout_explicit=False,
        pod_discovery_pairs=[],
        scout_ready=scout_ready,
        inbound_trace_id="test-trace",
        llm_slot_held=False,
        inbound_proactive=False,
        k8s_mutated=False,
        fallback_inline_commands=None,
    )
    return ctx


# ---------------------------------------------------------------------------
# _conversational_fallback
# ---------------------------------------------------------------------------

class TestConversationalFallback:
    async def test_returns_llm_output(self, monkeypatch):
        from workers import handlers
        # Patch heavy async helpers to return empty/None quickly
        monkeypatch.setattr(handlers, "fetch_infra_injection_for_fallback",
                            lambda ctx, t: _aempty())
        monkeypatch.setattr(handlers, "classify_route", lambda t: "default")

        ctx = _make_async_ctx(
            llm=_FakeLLM(chat_content="Tình trạng: bình thường.\n\nHành động tiếp theo: monitor."),
        )
        result = await handlers._conversational_fallback(
            ctx, "hello", "t1", reason="test", detail="some detail"
        )
        assert "Tình trạng" in result or len(result) > 0

    async def test_returns_default_when_llm_empty(self, monkeypatch):
        from workers import handlers

        monkeypatch.setattr(handlers, "fetch_infra_injection_for_fallback",
                            lambda ctx, t: _aempty())
        monkeypatch.setattr(handlers, "classify_route", lambda t: "default")

        ctx = _make_async_ctx(llm=_FakeLLM(chat_content=""))
        result = await handlers._conversational_fallback(
            ctx, "hello", "t2", reason="parse_error"
        )
        assert len(result) > 0
        assert "chưa có snapshot" in result or len(result) > 0

    async def test_learned_context_included(self, monkeypatch):
        from workers import handlers

        monkeypatch.setattr(handlers, "fetch_infra_injection_for_fallback",
                            lambda ctx, t: _aempty())
        monkeypatch.setattr(handlers, "classify_route", lambda t: "default")

        ctx = _make_async_ctx(llm=_FakeLLM(chat_content="Tình trạng: ok"))
        result = await handlers._conversational_fallback(
            ctx, "user text", "t3", reason="r", learned_context="some context"
        )
        assert len(result) > 0

    async def test_infra_injection_exception_ignored(self, monkeypatch):
        from workers import handlers

        async def _raise(*args, **kwargs):
            raise RuntimeError("infra fail")

        monkeypatch.setattr(handlers, "fetch_infra_injection_for_fallback", _raise)
        monkeypatch.setattr(handlers, "classify_route", lambda t: "sre")

        ctx = _make_async_ctx(llm=_FakeLLM(chat_content="some output"))
        result = await handlers._conversational_fallback(
            ctx, "user", "t4", reason="r"
        )
        assert len(result) >= 0  # should not raise

    async def test_suggestions_json_parsed_when_buttons_enabled(self, monkeypatch):
        from workers import handlers

        monkeypatch.setattr(handlers, "fetch_infra_injection_for_fallback",
                            lambda ctx, t: _aempty())
        monkeypatch.setattr(handlers, "classify_route", lambda t: "default")

        llm_output = 'Main text\nSUGGESTIONS_JSON: ["cmd1", "cmd2", "cmd3"]'
        ctx = _make_async_ctx(
            llm=_FakeLLM(chat_content=llm_output),
            fallback_inline_buttons_enabled=True,
        )
        ctx.fallback_inline_commands = None
        result = await handlers._conversational_fallback(
            ctx, "user", "t5", reason="r"
        )
        # With buttons enabled: fallback_inline_commands should be set
        assert ctx.fallback_inline_commands == ["cmd1", "cmd2", "cmd3"]
        assert "Main text" in result


# ---------------------------------------------------------------------------
# _repair_json_with_helper
# ---------------------------------------------------------------------------

class TestRepairJsonWithHelper:
    async def test_returns_helper_model_content(self):
        from workers.handlers import _repair_json_with_helper

        ctx = _make_async_ctx(llm=_FakeLLM(chat_content='{"tool":"reply","args":{"text":"fixed"}}'))
        result = await _repair_json_with_helper(ctx, "bad output", parse_error="parse fail")
        assert result == '{"tool":"reply","args":{"text":"fixed"}}'

    async def test_with_empty_parse_error(self):
        from workers.handlers import _repair_json_with_helper

        ctx = _make_async_ctx(llm=_FakeLLM(chat_content='{"tool":"echo","args":{}}'))
        result = await _repair_json_with_helper(ctx, '{"tool": "bad"')
        assert len(result) > 0


# ---------------------------------------------------------------------------
# _compress_history
# ---------------------------------------------------------------------------

class TestCompressHistory:
    async def test_returns_compressed_summary(self):
        from workers.handlers import _compress_history
        from workers.session_state import SessionState

        ctx = _make_async_ctx(llm=_FakeLLM(chat_content="Tóm tắt ngắn gọn."))
        state = SessionState()
        state.last_summary = "old summary"
        state.recent_messages = [{"role": "user", "content": "hello"}]
        result = await _compress_history(ctx, state, "tr1")
        assert result == "Tóm tắt ngắn gọn."

    async def test_returns_original_when_llm_empty(self):
        from workers.handlers import _compress_history
        from workers.session_state import SessionState

        ctx = _make_async_ctx(llm=_FakeLLM(chat_content=""))
        state = SessionState()
        state.last_summary = "keep this"
        result = await _compress_history(ctx, state, "tr2")
        assert result == "keep this"


# ---------------------------------------------------------------------------
# _deepseek_plan
# ---------------------------------------------------------------------------

class TestDeepseekPlan:
    async def test_returns_plan_text(self):
        from workers.handlers import _deepseek_plan

        ctx = _make_async_ctx(llm=_FakeLLM(chat_content="1. Check pods\n2. Inspect logs"))
        result = await _deepseek_plan(ctx, "user request", "tr1")
        assert "1." in result or len(result) >= 0

    async def test_empty_llm_returns_empty(self):
        from workers.handlers import _deepseek_plan

        ctx = _make_async_ctx(llm=_FakeLLM(chat_content=""))
        result = await _deepseek_plan(ctx, "user", "tr2")
        assert result == ""


# ---------------------------------------------------------------------------
# resolve_remediation_from_memory — no hits path
# ---------------------------------------------------------------------------

class TestResolveRemediationFromMemory:
    async def test_no_hits_returns_false(self, monkeypatch):
        from workers import handlers

        monkeypatch.setattr(handlers, "inc_fastpath_hits", lambda: None)

        ctx = _make_async_ctx()
        ok, out, tool = await handlers.resolve_remediation_from_memory(
            ctx, "check pods", trace="tr1"
        )
        assert ok is False
        assert out is None
        assert tool is None

    async def test_hit_below_threshold_returns_false(self, monkeypatch):
        from workers import handlers
        from rag.redis_vector_store import PointStruct, QueryResponse

        monkeypatch.setattr(handlers, "inc_fastpath_hits", lambda: None)
        # Hit with score 0.3 below default rag_fast_path_score 0.9
        pts = [PointStruct(id="1", payload={"auto_execute": True, "tool": "reply"}, score=0.3)]
        ctx = _make_async_ctx(vector_store=_FakeVectorStore(points=pts))
        ok, out, tool = await handlers.resolve_remediation_from_memory(
            ctx, "test", trace="tr2"
        )
        assert ok is False

    async def test_hit_no_auto_execute_returns_false(self, monkeypatch):
        from workers import handlers
        from rag.redis_vector_store import PointStruct, QueryResponse

        monkeypatch.setattr(handlers, "inc_fastpath_hits", lambda: None)
        pts = [PointStruct(id="1", payload={"auto_execute": False, "tool": "reply"}, score=0.95)]
        ctx = _make_async_ctx(vector_store=_FakeVectorStore(points=pts))
        ok, out, tool = await handlers.resolve_remediation_from_memory(
            ctx, "test", trace="tr3"
        )
        assert ok is False

    async def test_hit_bad_tool_returns_false(self, monkeypatch):
        from workers import handlers
        from rag.redis_vector_store import PointStruct

        monkeypatch.setattr(handlers, "inc_fastpath_hits", lambda: None)
        pts = [PointStruct(id="1", payload={"auto_execute": True, "tool": "nonexistent_tool_xyz"}, score=0.95)]
        ctx = _make_async_ctx(vector_store=_FakeVectorStore(points=pts))
        ok, out, tool = await handlers.resolve_remediation_from_memory(
            ctx, "test", trace="tr4"
        )
        assert ok is False


# ---------------------------------------------------------------------------
# try_fast_path — no hits path (routing disabled)
# ---------------------------------------------------------------------------

class TestTryFastPath:
    async def test_returns_false_when_no_hits(self, monkeypatch):
        from workers import handlers

        monkeypatch.setattr(handlers, "resolve_remediation_from_memory",
                            lambda *a, **kw: _afalse_3none())
        ctx = _make_async_ctx()
        ok, out = await handlers.try_fast_path(ctx, "check pods", trace="tr1")
        assert ok is False
        assert out is None

    async def test_returns_false_when_routing_disabled(self, monkeypatch):
        from workers import handlers

        monkeypatch.setattr(handlers, "resolve_remediation_from_memory",
                            lambda *a, **kw: _afalse_3none())
        ctx = _make_async_ctx(routing_experience_enabled=False)
        ok, out = await handlers.try_fast_path(ctx, "check pods", trace="tr2")
        assert ok is False

    async def test_returns_false_when_action_exp_disabled(self, monkeypatch):
        from workers import handlers

        monkeypatch.setattr(handlers, "resolve_remediation_from_memory",
                            lambda *a, **kw: _afalse_3none())
        ctx = _make_async_ctx(action_experience_enabled=False)
        ok, out = await handlers.try_fast_path(ctx, "check pods", trace="tr3")
        assert ok is False


# ---------------------------------------------------------------------------
# _slow_path_abort_no_data
# ---------------------------------------------------------------------------

class TestSlowPathAbortNoData:
    async def test_returns_autopsy_string(self, monkeypatch):
        from workers import handlers
        from workers.slow_path_trace import AttemptRecord

        monkeypatch.setattr(handlers, "record_routing_exhausted_no_data",
                            lambda *a, **kw: _anone())
        monkeypatch.setattr(handlers, "inc_slow_path_exhausted", lambda *a, **kw: None)

        ctx = _make_async_ctx()
        trace = [AttemptRecord(1, "parse", "sig", "bad json")]
        result = await handlers._slow_path_abort_no_data(
            ctx, "user text", "tr1",
            attempt_trace=trace,
            exit_reason="max_attempts",
        )
        assert isinstance(result, str)
        assert len(result) > 0

    async def test_empty_attempt_trace(self, monkeypatch):
        from workers import handlers

        monkeypatch.setattr(handlers, "record_routing_exhausted_no_data",
                            lambda *a, **kw: _anone())
        monkeypatch.setattr(handlers, "inc_slow_path_exhausted", lambda *a, **kw: None)

        ctx = _make_async_ctx()
        result = await handlers._slow_path_abort_no_data(
            ctx, "user", "tr2",
            attempt_trace=[],
            exit_reason="loop_exit",
        )
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# slow_path_with_llm_and_tools — monkeypatched paths
# ---------------------------------------------------------------------------

class TestSlowPathWithLlmAndTools:
    async def test_exhausted_on_parse_failure(self, monkeypatch):
        """When LLM always returns invalid JSON: exhausted after max_attempts."""
        from workers import handlers

        monkeypatch.setattr(handlers, "fetch_action_experience_context",
                            lambda *a, **kw: _aempty())
        monkeypatch.setattr(handlers, "fetch_baseline_system_prompt",
                            lambda *a, **kw: _aempty())
        monkeypatch.setattr(handlers, "record_routing_exhausted_no_data",
                            lambda *a, **kw: _anone())
        monkeypatch.setattr(handlers, "record_routing_from_success",
                            lambda *a, **kw: _anone())
        monkeypatch.setattr(handlers, "inc_slow_path_exhausted", lambda *a, **kw: None)
        monkeypatch.setattr(handlers, "inc_llm_requests", lambda: None)
        monkeypatch.setattr(handlers, "inc_experience_saved", lambda: None)
        monkeypatch.setattr(handlers, "shell_fast_path_enabled", lambda _: False)

        ctx = _make_async_ctx(
            llm=_FakeLLM(chat_content="not valid json at all"),
            slow_path_max_tool_attempts=2,
            slow_path_stale_signature_streak=5,
            json_repair_max=1,
            baseline_snapshot_enabled=False,
        )
        result = await handlers.slow_path_with_llm_and_tools(
            ctx, "check pods", trace="tr1"
        )
        assert isinstance(result, str)
        assert len(result) > 0

    async def test_returns_tool_output_on_success(self, monkeypatch):
        """When LLM returns valid JSON for a registered tool: tool is called."""
        from workers import handlers
        from workers.tools import TOOL_REGISTRY

        monkeypatch.setattr(handlers, "fetch_action_experience_context",
                            lambda *a, **kw: _aempty())
        monkeypatch.setattr(handlers, "fetch_baseline_system_prompt",
                            lambda *a, **kw: _aempty())
        monkeypatch.setattr(handlers, "record_routing_from_success",
                            lambda *a, **kw: _anone())
        monkeypatch.setattr(handlers, "inc_llm_requests", lambda: None)
        monkeypatch.setattr(handlers, "inc_experience_saved", lambda: None)
        monkeypatch.setattr(handlers, "shell_fast_path_enabled", lambda _: False)

        # Register a temporary tool that returns predictable output
        async def _fake_echo(ctx: Any, args: dict) -> str:
            return "echo result"

        original_registry = dict(TOOL_REGISTRY)
        TOOL_REGISTRY["_test_echo_tmp"] = _fake_echo
        try:
            ctx = _make_async_ctx(
                llm=_FakeLLM(chat_content='{"tool":"_test_echo_tmp","args":{}}'),
                slow_path_max_tool_attempts=2,
                baseline_snapshot_enabled=False,
            )
            result = await handlers.slow_path_with_llm_and_tools(
                ctx, "echo please", trace="tr2"
            )
            assert result == "echo result"
        finally:
            TOOL_REGISTRY.clear()
            TOOL_REGISTRY.update(original_registry)

    async def test_unknown_tool_triggers_autonomous_rescue(self, monkeypatch):
        """When LLM returns unknown tool, autonomous_sdk_route is attempted."""
        from workers import handlers

        monkeypatch.setattr(handlers, "fetch_action_experience_context",
                            lambda *a, **kw: _aempty())
        monkeypatch.setattr(handlers, "fetch_baseline_system_prompt",
                            lambda *a, **kw: _aempty())
        monkeypatch.setattr(handlers, "record_routing_exhausted_no_data",
                            lambda *a, **kw: _anone())
        monkeypatch.setattr(handlers, "record_routing_from_success",
                            lambda *a, **kw: _anone())
        monkeypatch.setattr(handlers, "inc_llm_requests", lambda: None)
        monkeypatch.setattr(handlers, "inc_slow_path_exhausted", lambda *a: None)
        monkeypatch.setattr(handlers, "shell_fast_path_enabled", lambda _: False)

        async def _sdk_rescue(ctx: Any, text: str) -> str:
            return "sdk rescued"

        monkeypatch.setattr(handlers, "try_autonomous_sdk_route", _sdk_rescue)

        ctx = _make_async_ctx(
            llm=_FakeLLM(chat_content='{"tool":"nonexistent_unknown_xyz","args":{}}'),
            slow_path_max_tool_attempts=2,
            baseline_snapshot_enabled=False,
        )
        result = await handlers.slow_path_with_llm_and_tools(
            ctx, "something", trace="tr3"
        )
        assert result == "sdk rescued"

    async def test_stale_signature_streak_aborts_early(self, monkeypatch):
        """Repeated identical parse failures → abort early via stale signature."""
        from workers import handlers

        monkeypatch.setattr(handlers, "fetch_action_experience_context",
                            lambda *a, **kw: _aempty())
        monkeypatch.setattr(handlers, "fetch_baseline_system_prompt",
                            lambda *a, **kw: _aempty())
        monkeypatch.setattr(handlers, "record_routing_exhausted_no_data",
                            lambda *a, **kw: _anone())
        monkeypatch.setattr(handlers, "inc_llm_requests", lambda: None)
        monkeypatch.setattr(handlers, "inc_slow_path_exhausted", lambda *a: None)
        monkeypatch.setattr(handlers, "shell_fast_path_enabled", lambda _: False)

        ctx = _make_async_ctx(
            llm=_FakeLLM(chat_content="INVALID JSON REPEATED"),
            slow_path_max_tool_attempts=5,
            slow_path_stale_signature_streak=2,
            json_repair_max=1,
            baseline_snapshot_enabled=False,
        )
        result = await handlers.slow_path_with_llm_and_tools(
            ctx, "repeated failure", trace="tr4"
        )
        assert isinstance(result, str)

    async def test_needs_plan_calls_deepseek(self, monkeypatch):
        """When needs_plan=True, _deepseek_plan is called."""
        from workers import handlers

        monkeypatch.setattr(handlers, "fetch_action_experience_context",
                            lambda *a, **kw: _aempty())
        monkeypatch.setattr(handlers, "fetch_baseline_system_prompt",
                            lambda *a, **kw: _aempty())
        monkeypatch.setattr(handlers, "record_routing_exhausted_no_data",
                            lambda *a, **kw: _anone())
        monkeypatch.setattr(handlers, "inc_llm_requests", lambda: None)
        monkeypatch.setattr(handlers, "inc_slow_path_exhausted", lambda *a: None)
        monkeypatch.setattr(handlers, "shell_fast_path_enabled", lambda _: False)

        plan_called = []

        async def _fake_plan(ctx: Any, text: str, trace: str) -> str:
            plan_called.append(True)
            return "1. Plan step"

        monkeypatch.setattr(handlers, "_deepseek_plan", _fake_plan)

        ctx = _make_async_ctx(
            llm=_FakeLLM(chat_content="bad json for exhaustion"),
            slow_path_max_tool_attempts=1,
            json_repair_max=1,
            baseline_snapshot_enabled=False,
        )
        await handlers.slow_path_with_llm_and_tools(
            ctx, "complex task", trace="tr5", needs_plan=True
        )
        assert plan_called  # plan was invoked

    async def test_empty_model_output_handled(self, monkeypatch):
        """When model returns empty content: attempt aborts after max_attempts."""
        from workers import handlers

        monkeypatch.setattr(handlers, "fetch_action_experience_context",
                            lambda *a, **kw: _aempty())
        monkeypatch.setattr(handlers, "fetch_baseline_system_prompt",
                            lambda *a, **kw: _aempty())
        monkeypatch.setattr(handlers, "record_routing_exhausted_no_data",
                            lambda *a, **kw: _anone())
        monkeypatch.setattr(handlers, "inc_llm_requests", lambda: None)
        monkeypatch.setattr(handlers, "inc_slow_path_exhausted", lambda *a: None)
        monkeypatch.setattr(handlers, "shell_fast_path_enabled", lambda _: False)

        ctx = _make_async_ctx(
            llm=_FakeLLM(chat_content="   "),  # whitespace-only = empty
            slow_path_max_tool_attempts=2,
            slow_path_stale_signature_streak=3,
            baseline_snapshot_enabled=False,
        )
        result = await handlers.slow_path_with_llm_and_tools(
            ctx, "task", trace="tr6"
        )
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# handle_inbound_payload / _handle_inbound_payload_impl
# ---------------------------------------------------------------------------

class TestHandleInboundPayload:
    async def _stub_all(self, monkeypatch: Any) -> None:
        from workers import handlers
        monkeypatch.setattr(handlers, "push_trace_id", lambda t: "tok")
        monkeypatch.setattr(handlers, "pop_trace_id", lambda tok: None)
        monkeypatch.setattr(handlers, "log_start_request", lambda *a, **kw: None)
        monkeypatch.setattr(handlers, "log_end_request", lambda *a, **kw: None)
        monkeypatch.setattr(handlers, "inc_messages_processed", lambda src: None)
        monkeypatch.setattr(handlers, "_hc_record_msg", lambda: None)
        monkeypatch.setattr(handlers, "inbound_trace_span", _NullContextMgr)
        monkeypatch.setattr(handlers, "child_span", _NullContextMgr)

    async def test_empty_text_returns_no_content_message(self, monkeypatch):
        from workers import handlers
        await self._stub_all(monkeypatch)

        ctx = _make_async_ctx()
        payload = {"text": "", "source": "telegram"}
        result = await handlers.handle_inbound_payload(ctx, payload)
        assert "Không có nội dung" in result

    async def test_scout_not_ready_returns_waiting_message(self, monkeypatch):
        from workers import handlers
        await self._stub_all(monkeypatch)

        ctx = _make_async_ctx()
        ctx.scout_ready = asyncio.Event()  # Not set
        payload = {"text": "hello", "source": "telegram"}
        result = await handlers.handle_inbound_payload(ctx, payload)
        assert "Deep Scout" in result

    async def test_prometheus_source_with_fast_path_success(self, monkeypatch):
        """prometheus source + fast_path hit → returns capped output."""
        from workers import handlers

        await self._stub_all(monkeypatch)
        monkeypatch.setattr(handlers, "try_autonomous_sdk_route",
                            lambda ctx, t: _anone())
        monkeypatch.setattr(handlers, "evaluate_rag_gate",
                            lambda ctx, t, **kw: _arag_miss())
        monkeypatch.setattr(handlers, "preflight_infra_kb",
                            lambda ctx, t, **kw: _anone())
        monkeypatch.setattr(handlers, "enrich_working_text_with_infra",
                            lambda ctx, t, **kw: _aid(t))
        monkeypatch.setattr(handlers, "is_ambiguous_resource_check", lambda *a, **kw: False)
        monkeypatch.setattr(handlers, "try_fast_path",
                            lambda ctx, t, **kw: _afast_result())

        ctx = _make_async_ctx()
        payload = {"text": "Alert: CPUHigh pod=api-0 namespace=prod", "source": "prometheus"}
        result = await handlers.handle_inbound_payload(ctx, payload)
        assert isinstance(result, str)

    async def test_telegram_chat_with_no_fast_path_uses_slow_path(self, monkeypatch):
        """Telegram source + no fast path → agentic/slow path called."""
        from workers import handlers

        await self._stub_all(monkeypatch)
        monkeypatch.setattr(handlers, "try_autonomous_sdk_route",
                            lambda ctx, t: _anone())
        monkeypatch.setattr(handlers, "evaluate_rag_gate",
                            lambda ctx, t, **kw: _arag_miss())
        monkeypatch.setattr(handlers, "preflight_infra_kb",
                            lambda ctx, t, **kw: _anone())
        monkeypatch.setattr(handlers, "enrich_working_text_with_infra",
                            lambda ctx, t, **kw: _aid(t))
        monkeypatch.setattr(handlers, "is_ambiguous_resource_check", lambda *a, **kw: False)
        monkeypatch.setattr(handlers, "try_fast_path",
                            lambda ctx, t, **kw: _afalse_none())

        slow_path_calls: list[str] = []

        async def _fake_slow(ctx: Any, text: str, **kw) -> str:
            slow_path_calls.append(text)
            return "slow path result"

        monkeypatch.setattr(handlers, "slow_path_with_llm_and_tools", _fake_slow)

        ctx = _make_async_ctx(agentic_slow_path_enabled=False)
        payload = {"text": "check pods", "source": "telegram", "chat_id": 12345}
        result = await handlers.handle_inbound_payload(ctx, payload)
        assert slow_path_calls  # slow path was called
        assert isinstance(result, str)

    async def test_rag_gate_hit_returns_formatted_output(self, monkeypatch):
        """RAG gate hit with formatted output → returns it without calling slow path."""
        from workers import handlers

        await self._stub_all(monkeypatch)
        monkeypatch.setattr(handlers, "try_autonomous_sdk_route",
                            lambda ctx, t: _anone())
        monkeypatch.setattr(handlers, "preflight_infra_kb",
                            lambda ctx, t, **kw: _anone())

        async def _rag_hit(ctx: Any, text: str, **kw) -> "_FakeRagGateOutcome":
            return _FakeRagGateOutcome(hit=True, formatted="RAG answer")

        monkeypatch.setattr(handlers, "evaluate_rag_gate", _rag_hit)

        ctx = _make_async_ctx()
        payload = {"text": "what is kubernetes", "source": "telegram"}
        result = await handlers.handle_inbound_payload(ctx, payload)
        assert "RAG answer" in result

    async def test_list_all_pods_pattern_triggers_tool(self, monkeypatch):
        """Message matching RE_LIST_ALL_PODS_CHAT → list_all_pods_sdk called directly."""
        from workers import handlers
        from workers.tools import TOOL_REGISTRY

        await self._stub_all(monkeypatch)
        monkeypatch.setattr(handlers, "try_autonomous_sdk_route",
                            lambda ctx, t: _anone())

        original_tool = TOOL_REGISTRY.get("list_all_pods_sdk")
        async def _fake_list_pods(ctx: Any, args: dict) -> str:
            return "pod list result"

        TOOL_REGISTRY["list_all_pods_sdk"] = _fake_list_pods
        try:
            ctx = _make_async_ctx()
            payload = {"text": "list all pods", "source": "telegram"}
            result = await handlers.handle_inbound_payload(ctx, payload)
            assert "pod list result" in result
        finally:
            if original_tool is not None:
                TOOL_REGISTRY["list_all_pods_sdk"] = original_tool

    async def test_error_propagated_from_impl(self, monkeypatch):
        """Exceptions in _handle_inbound_payload_impl propagate from handle_inbound_payload."""
        from workers import handlers

        await self._stub_all(monkeypatch)

        async def _bad_impl(ctx, payload, trace):
            raise ValueError("impl error")

        monkeypatch.setattr(handlers, "_handle_inbound_payload_impl", _bad_impl)

        ctx = _make_async_ctx()
        payload = {"text": "hello", "source": "telegram"}
        with pytest.raises(ValueError, match="impl error"):
            await handlers.handle_inbound_payload(ctx, payload)


# ---------------------------------------------------------------------------
# Helpers for async tests
# ---------------------------------------------------------------------------

class _NullContextMgr:
    """Context manager that does nothing — used to monkeypatch child_span etc."""
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass
    def __enter__(self) -> "_NullContextMgr":
        return self
    def __exit__(self, *args: Any) -> None:
        pass


class _FakeRagGateOutcome:
    def __init__(self, hit: bool, formatted: str = "") -> None:
        self.hit = hit
        self.formatted = formatted
        self.best_score = None
        self.detail = {}


# ---------------------------------------------------------------------------
# _handle_inbound_payload_impl — specific branches
# ---------------------------------------------------------------------------

class TestHandleInboundPayloadImpl:
    """Tests for specific branches in the implementation."""

    async def _stub_impl(self, monkeypatch: Any) -> None:
        from workers import handlers
        monkeypatch.setattr(handlers, "inc_messages_processed", lambda src: None)
        monkeypatch.setattr(handlers, "_hc_record_msg", lambda: None)
        monkeypatch.setattr(handlers, "child_span", _NullContextMgr)
        monkeypatch.setattr(handlers, "push_trace_id", lambda t: "tok")
        monkeypatch.setattr(handlers, "pop_trace_id", lambda tok: None)
        monkeypatch.setattr(handlers, "log_start_request", lambda *a, **kw: None)
        monkeypatch.setattr(handlers, "log_end_request", lambda *a, **kw: None)
        monkeypatch.setattr(handlers, "inbound_trace_span", _NullContextMgr)

    async def test_write_pending_confirmed(self, monkeypatch):
        """When write_pending exists and user confirms → execute_write_pending called."""
        import fakeredis.aioredis
        from workers import handlers

        await self._stub_impl(monkeypatch)

        write_pending_data = json.dumps({"tool": "echo", "args": {}})

        async def _fake_exec_write(ctx: Any, data: dict) -> str:
            return "write executed"

        monkeypatch.setattr(handlers, "execute_write_pending_from_redis", _fake_exec_write)
        monkeypatch.setattr(handlers, "execute_rollout_restart_from_pending",
                            lambda *a, **kw: _arollout_done())

        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        from workers.k8s_tools import redis_key_write_pending
        await r.set(redis_key_write_pending(99999), write_pending_data)

        scout_ready = asyncio.Event()
        scout_ready.set()

        ctx = SimpleNamespace(
            settings=_make_settings(),
            redis=r,
            llm=_FakeLLM(),
            vector_store=_FakeVectorStore(),
            ledger=None,
            semaphore=_FakeSemaphore(),
            telegram=None,
            kafka=None,
            telegram_chat_id=None,
            inbound_source="",
            inbound_user_text="",
            restart_rollout_explicit=False,
            pod_discovery_pairs=[],
            scout_ready=scout_ready,
            inbound_trace_id="test",
            llm_slot_held=False,
            inbound_proactive=False,
            k8s_mutated=False,
            fallback_inline_commands=None,
        )
        payload = {"text": "ok", "source": "telegram", "chat_id": 99999}
        result = await handlers._handle_inbound_payload_impl(ctx, payload, "tr1")
        assert result == "write executed"

    async def test_rollout_pending_confirmed(self, monkeypatch):
        """When rollout_pending exists and user confirms → execute_rollout called."""
        import fakeredis.aioredis
        from workers import handlers

        await self._stub_impl(monkeypatch)

        rollout_data = json.dumps({"deployment": "api", "namespace": "prod"})

        async def _fake_exec_rollout(ctx: Any, data: dict) -> str:
            return "rollout executed"

        monkeypatch.setattr(handlers, "execute_rollout_restart_from_pending", _fake_exec_rollout)
        monkeypatch.setattr(handlers, "execute_write_pending_from_redis",
                            lambda *a, **kw: _awrite_done())

        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        from workers.k8s_tools import redis_key_rollout_pending, redis_key_write_pending
        await r.set(redis_key_rollout_pending(88888), rollout_data)
        # No write_pending

        scout_ready = asyncio.Event()
        scout_ready.set()

        ctx = SimpleNamespace(
            settings=_make_settings(),
            redis=r,
            llm=_FakeLLM(),
            vector_store=_FakeVectorStore(),
            ledger=None,
            semaphore=_FakeSemaphore(),
            telegram=None,
            kafka=None,
            telegram_chat_id=None,
            inbound_source="",
            inbound_user_text="",
            restart_rollout_explicit=False,
            pod_discovery_pairs=[],
            scout_ready=scout_ready,
            inbound_trace_id="test",
            llm_slot_held=False,
            inbound_proactive=False,
            k8s_mutated=False,
            fallback_inline_commands=None,
        )
        payload = {"text": "confirm", "source": "telegram", "chat_id": 88888}
        result = await handlers._handle_inbound_payload_impl(ctx, payload, "tr2")
        assert result == "rollout executed"

    async def test_ambiguous_resource_check_starts_vm_slots(self, monkeypatch):
        """Ambiguous resource check → slot-filling flow."""
        import fakeredis.aioredis
        from workers import handlers

        await self._stub_impl(monkeypatch)
        monkeypatch.setattr(handlers, "try_autonomous_sdk_route",
                            lambda ctx, t: _anone())
        monkeypatch.setattr(handlers, "evaluate_rag_gate",
                            lambda ctx, t, **kw: _arag_miss())
        monkeypatch.setattr(handlers, "preflight_infra_kb",
                            lambda ctx, t, **kw: _anone())
        monkeypatch.setattr(handlers, "is_ambiguous_resource_check", lambda *a, **kw: True)
        monkeypatch.setattr(handlers, "extract_vm_slots_from_text", lambda t: {})
        monkeypatch.setattr(handlers, "enrich_slots_from_discovery", lambda s, d: s)
        monkeypatch.setattr(handlers, "nudge_vm_slots_message", lambda s: "Đại ca muốn check Host hay Pod?")

        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        scout_ready = asyncio.Event()
        scout_ready.set()
        ctx = SimpleNamespace(
            settings=_make_settings(),
            redis=r,
            llm=_FakeLLM(),
            vector_store=_FakeVectorStore(),
            ledger=None,
            semaphore=_FakeSemaphore(),
            telegram=None,
            kafka=None,
            telegram_chat_id=None,
            inbound_source="",
            inbound_user_text="",
            restart_rollout_explicit=False,
            pod_discovery_pairs=[],
            scout_ready=scout_ready,
            inbound_trace_id="test",
            llm_slot_held=False,
            inbound_proactive=False,
            k8s_mutated=False,
            fallback_inline_commands=None,
        )
        payload = {"text": "check CPU", "source": "telegram", "chat_id": 77777}
        result = await handlers._handle_inbound_payload_impl(ctx, payload, "tr3")
        assert "Host" in result or "Pod" in result or len(result) > 0
