"""Track 0A — pure policy / pure-logic tests.

Covers:
  - src/workers/clarification_context.py  (target: ~100%)
  - src/workers/tool_approval.py          (target: ~100%)
  - src/workers/advisory_mode_kill_switch.py (target: ~100%)
  - src/workers/adapters/contracts.py     (target: ~100%)

Constraints:
  - No unittest.mock.patch / MagicMock / AsyncMock on business logic.
  - Use fakeredis.aioredis.FakeRedis(decode_responses=True) for Redis.
  - Use _KafkaCapture inline for Kafka.
  - Use SimpleNamespace for ctx.
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace

import fakeredis.aioredis


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

class _KafkaCapture:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_dict(self, topic: str, payload: dict, *, key: bytes | None = None) -> None:
        self.sent.append((topic, payload))


def _make_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _make_ctx(redis=None, kafka=None, settings=None):
    return SimpleNamespace(
        redis=redis or _make_redis(),
        kafka=kafka or _KafkaCapture(),
        settings=settings,
    )


# ===========================================================================
# 1. clarification_context.py
# ===========================================================================

class TestStripJsonFence:
    """Unit tests for _strip_json_fence."""

    def _fn(self, s: str) -> str:
        from workers.clarification_context import _strip_json_fence
        return _strip_json_fence(s)

    def test_plain_json_unchanged(self):
        raw = '{"target": "host"}'
        assert self._fn(raw) == raw

    def test_strips_triple_backtick_fence(self):
        raw = "```\n{\"target\": \"pod\"}\n```"
        result = self._fn(raw)
        assert result == '{"target": "pod"}'

    def test_strips_json_labelled_fence(self):
        raw = "```json\n{\"target\": \"namespace\"}\n```"
        result = self._fn(raw)
        assert result == '{"target": "namespace"}'

    def test_no_trailing_fence_left_alone(self):
        # fence opens but doesn't close — still strips opening line
        raw = "```\n{\"target\": \"host\"}"
        result = self._fn(raw)
        assert result == '{"target": "host"}'

    def test_empty_string_returns_empty(self):
        assert self._fn("   ") == ""

    def test_whitespace_stripped_around_plain(self):
        raw = "  {\"target\": \"unclear\"}  "
        assert self._fn(raw) == '{"target": "unclear"}'


class TestFormatSessionSnippetForLlm:
    """Unit tests for format_session_snippet_for_llm."""

    def _fn(self, **kwargs):
        from workers.clarification_context import format_session_snippet_for_llm
        return format_session_snippet_for_llm(**kwargs)

    def test_empty_inputs_returns_empty(self):
        result = self._fn(last_summary="", recent_messages=[])
        assert result == ""

    def test_summary_only(self):
        result = self._fn(last_summary="user wants pod logs", recent_messages=[])
        assert "[summary]" in result
        assert "user wants pod logs" in result

    def test_messages_only(self):
        msgs = [
            {"role": "user", "content": "show me the errors"},
            {"role": "assistant", "content": "which pod?"},
        ]
        result = self._fn(last_summary="", recent_messages=msgs)
        assert "user:" in result
        assert "assistant:" in result

    def test_summary_truncated_at_900(self):
        long_summary = "x" * 2000
        result = self._fn(last_summary=long_summary, recent_messages=[])
        # Only 900 chars kept
        assert len(result) <= len("[summary] ") + 900

    def test_only_last_six_messages_included(self):
        msgs = [{"role": "user", "content": f"msg{i}"} for i in range(10)]
        result = self._fn(last_summary="", recent_messages=msgs)
        # msg0..msg3 should be excluded, msg4..msg9 kept
        assert "msg4" in result
        assert "msg0" not in result

    def test_message_content_truncated_at_500(self):
        msgs = [{"role": "user", "content": "a" * 1000}]
        result = self._fn(last_summary="", recent_messages=msgs)
        assert "a" * 501 not in result

    def test_combined_summary_and_messages(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = self._fn(last_summary="Monitoring overview", recent_messages=msgs)
        assert "[summary]" in result
        assert "hello" in result


class TestFollowupLlmToMergeParams:
    """Unit tests for followup_llm_to_merge_params."""

    def _fn(self, **kwargs):
        from workers.clarification_context import MonitoringFollowupLLM, followup_llm_to_merge_params
        m = MonitoringFollowupLLM(**kwargs)
        return followup_llm_to_merge_params(m)

    def test_unclear_returns_none(self):
        assert self._fn(target="unclear") is None

    def test_host_returns_host_tuple(self):
        result = self._fn(target="host")
        assert result is not None
        kind, pod, ns = result
        assert kind == "host"
        assert pod is None

    def test_host_with_namespace(self):
        result = self._fn(target="host", namespace="production")
        assert result is not None
        kind, pod, ns = result
        assert kind == "host"
        assert ns == "production"

    def test_namespace_target(self):
        result = self._fn(target="namespace", namespace="staging")
        assert result is not None
        kind, pod, ns = result
        assert kind == "namespace"
        assert pod is None
        assert ns == "staging"

    def test_pod_with_name(self):
        result = self._fn(target="pod", pod_name="nginx-abc123")
        assert result is not None
        kind, pod, ns = result
        assert kind == "pod"
        assert pod == "nginx-abc123"

    def test_pod_without_name_returns_none_pod(self):
        result = self._fn(target="pod", pod_name="  ")
        assert result is not None
        kind, pod, ns = result
        assert kind == "pod"
        assert pod is None

    def test_pod_with_namespace(self):
        result = self._fn(target="pod", pod_name="api-xyz", namespace="default")
        assert result is not None
        kind, pod, ns = result
        assert kind == "pod"
        assert pod == "api-xyz"
        assert ns == "default"


class TestInterpretMonitoringFollowupLlm:
    """Tests for interpret_monitoring_followup_llm — LLM absent path (returns None)."""

    @pytest.mark.asyncio
    async def test_returns_none_when_llm_absent(self):
        from workers.clarification_context import interpret_monitoring_followup_llm
        ctx = SimpleNamespace()  # no llm, no settings
        result = await interpret_monitoring_followup_llm(
            ctx,
            last_user_goal="show CPU usage",
            bot_question="host or pod?",
            user_reply="the host",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_settings_absent(self):
        from workers.clarification_context import interpret_monitoring_followup_llm

        class _FakeLlm:
            async def chat(self, **_kw):
                return {}

        ctx = SimpleNamespace(llm=_FakeLlm())  # settings missing
        result = await interpret_monitoring_followup_llm(
            ctx,
            last_user_goal="show CPU",
            bot_question="host or pod?",
            user_reply="pod",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_model_when_llm_returns_valid_json(self):
        from workers.clarification_context import interpret_monitoring_followup_llm, MonitoringFollowupLLM

        class _FakeLlm:
            async def chat(self, model, messages, options):
                return {"message": {"content": '{"target":"host","pod_name":null,"namespace":null}'}}

        ctx = SimpleNamespace(llm=_FakeLlm(), settings=SimpleNamespace(model_helper="qwen2.5-coder-3b"))
        result = await interpret_monitoring_followup_llm(
            ctx,
            last_user_goal="monitor CPU",
            bot_question="host or pod?",
            user_reply="the host node",
            recent_dialog_snippet="user asked about CPU spikes",
        )
        assert isinstance(result, MonitoringFollowupLLM)
        assert result.target == "host"

    @pytest.mark.asyncio
    async def test_returns_none_on_invalid_json_from_llm(self):
        from workers.clarification_context import interpret_monitoring_followup_llm

        class _FakeLlm:
            async def chat(self, model, messages, options):
                return {"message": {"content": "not-json-at-all"}}

        ctx = SimpleNamespace(llm=_FakeLlm(), settings=SimpleNamespace(model_helper=None))
        result = await interpret_monitoring_followup_llm(
            ctx,
            last_user_goal="monitor CPU",
            bot_question="host or pod?",
            user_reply="pod",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_llm_returns_non_dict(self):
        from workers.clarification_context import interpret_monitoring_followup_llm

        class _FakeLlm:
            async def chat(self, model, messages, options):
                return {"message": {"content": "[1,2,3]"}}

        ctx = SimpleNamespace(llm=_FakeLlm(), settings=SimpleNamespace(model_helper=None))
        result = await interpret_monitoring_followup_llm(
            ctx,
            last_user_goal="monitor",
            bot_question="?",
            user_reply="pod",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_snippet_truncated_at_2000(self):
        """Verify very long snippet is truncated (no crash)."""
        from workers.clarification_context import interpret_monitoring_followup_llm

        captured_msgs = []

        class _FakeLlm:
            async def chat(self, model, messages, options):
                captured_msgs.extend(messages)
                return {"message": {"content": '{"target":"unclear","pod_name":null,"namespace":null}'}}

        ctx = SimpleNamespace(llm=_FakeLlm(), settings=SimpleNamespace(model_helper="model"))
        long_snippet = "x" * 5000
        await interpret_monitoring_followup_llm(
            ctx,
            last_user_goal="goal",
            bot_question="question",
            user_reply="reply",
            recent_dialog_snippet=long_snippet,
        )
        # user message content should not have 5000 'x's
        user_content = captured_msgs[-1]["content"]
        assert "x" * 2001 not in user_content


# ===========================================================================
# 2. tool_approval.py
# ===========================================================================

class TestToolApprovalConstants:
    def test_approval_key_prefix_is_string(self):
        from workers.tool_approval import APPROVAL_KEY_PREFIX
        assert isinstance(APPROVAL_KEY_PREFIX, str)
        assert APPROVAL_KEY_PREFIX.startswith("omni:")

    @pytest.mark.asyncio
    async def test_approval_status_returns_none(self):
        """approval_status is deprecated and always returns None."""
        from workers.tool_approval import approval_status
        ctx = _make_ctx()
        result = await approval_status(ctx, "any-token-value")
        assert result is None

    @pytest.mark.asyncio
    async def test_approval_status_ignores_token_value(self):
        from workers.tool_approval import approval_status
        ctx = _make_ctx()
        for token in ("", "abc", "1234-abcd", "x" * 200):
            result = await approval_status(ctx, token)
            assert result is None

    @pytest.mark.asyncio
    async def test_request_approval_returns_false_and_escalates(self):
        """request_approval always returns False (deny-by-default) and calls escalation.

        tool_approval uses a local import inside request_approval:
          from workers.telegram_escalation import emit_telegram_escalation
        So we must patch the source module attribute, not the importer.
        """
        from workers.tool_approval import request_approval
        import unittest.mock

        escalation_calls: list[tuple] = []

        async def _fake_escalate(ctx, trace, body, *, reason):
            escalation_calls.append((trace, body, reason))

        # Patch the function where it lives (source module), not where it is imported.
        with unittest.mock.patch(
            "workers.telegram_escalation.emit_telegram_escalation",
            new=_fake_escalate,
        ):
            ctx = _make_ctx()
            ctx.inbound_trace_id = "trace-abc"
            result = await request_approval(
                ctx,
                tool_name="k8s_rollout_restart",
                args_summary="deployment=nginx",
                fp="ns=production",
            )
        assert result is False
        assert len(escalation_calls) == 1
        trace, body, reason = escalation_calls[0]
        assert trace == "trace-abc"
        assert "k8s_rollout_restart" in body
        assert reason == "LEGACY_APPROVAL_REQUEST"

    @pytest.mark.asyncio
    async def test_request_approval_uses_fallback_trace_when_no_inbound_trace(self):
        """When ctx lacks inbound_trace_id, falls back to 'approval-request'."""
        from workers.tool_approval import request_approval
        import unittest.mock

        escalation_calls: list[tuple] = []

        async def _fake_escalate(ctx, trace, body, *, reason):
            escalation_calls.append((trace, body, reason))

        with unittest.mock.patch(
            "workers.telegram_escalation.emit_telegram_escalation",
            new=_fake_escalate,
        ):
            ctx = _make_ctx()  # no inbound_trace_id
            result = await request_approval(
                ctx,
                tool_name="k8s_delete_pod",
                args_summary="pod=nginx-abc",
                fp="ns=staging",
            )
        assert result is False
        trace, _, _ = escalation_calls[0]
        assert trace == "approval-request"


# ===========================================================================
# 3. advisory_mode_kill_switch.py
# ===========================================================================

class TestAdvisoryModeKillSwitch_ValidateExecutionGate:
    """Tests for AdvisoryModeKillSwitch.validate_execution_gate (pure logic)."""

    def _gate(self, tool_name: str, args: dict, **kw):
        from workers.advisory_mode_kill_switch import AdvisoryModeKillSwitch
        return AdvisoryModeKillSwitch.validate_execution_gate(tool_name, args, **kw)

    def test_blocks_when_auto_execute_disabled(self):
        ok, reason = self._gate("k8s_rollout_restart", {}, auto_execute_enabled=False)
        assert ok is False
        assert "ADVISORY_MODE_KILL_SWITCH" in reason
        assert "k8s_rollout_restart" in reason

    def test_blocks_includes_context_in_reason(self):
        ok, reason = self._gate(
            "k8s_scale_deployment", {}, context="analyst", auto_execute_enabled=False
        )
        assert ok is False
        assert "analyst" in reason

    def test_allows_safe_tool_when_auto_execute_enabled(self):
        ok, reason = self._gate(
            "k8s_rollout_restart", {}, auto_execute_enabled=True, siem_suggest_only=False
        )
        assert ok is True
        assert reason == "execution_allowed"

    def test_blocks_dangerous_tool_even_when_auto_execute_enabled(self):
        dangerous = [
            "k8s_delete_pod",
            "k8s_delete_deployment",
            "k8s_delete_pvc",
            "k8s_patch_rbac",
            "k8s_patch_secret",
            "k8s_mutate_taint",
        ]
        for tool in dangerous:
            ok, reason = self._gate(tool, {}, auto_execute_enabled=True)
            assert ok is False, f"{tool} should be blocked"
            assert "SAFETY_GATE" in reason

    def test_safe_tool_not_in_dangerous_list(self):
        ok, reason = self._gate(
            "k8s_get_pod_logs", {}, auto_execute_enabled=True
        )
        assert ok is True

    def test_default_auto_execute_is_false(self):
        """Default parameter is False — fail-closed."""
        from workers.advisory_mode_kill_switch import AdvisoryModeKillSwitch
        ok, _ = AdvisoryModeKillSwitch.validate_execution_gate("any_tool", {})
        assert ok is False


class TestAdvisoryModeKillSwitch_ValidateAdvisorOutput:
    """Tests for validate_advisor_output (pure logic)."""

    def _validate(self, advisory_dict: dict):
        from workers.advisory_mode_kill_switch import AdvisoryModeKillSwitch
        return AdvisoryModeKillSwitch.validate_advisor_output(advisory_dict)

    def test_valid_advisory_no_forbidden_steps(self):
        advisory = {
            "proposed_remediation": [
                {"action": "kubectl rollout restart deployment/nginx"},
                {"action": "kubectl get pods -n production"},
            ]
        }
        ok, reason = self._validate(advisory)
        assert ok is True
        assert reason == "advisory_valid"

    def test_blocks_kubectl_delete(self):
        advisory = {
            "proposed_remediation": [
                {"action": "kubectl delete pod nginx-abc -n production"}
            ]
        }
        ok, reason = self._validate(advisory)
        assert ok is False
        assert "kubectl delete" in reason

    def test_blocks_kubectl_drain(self):
        advisory = {
            "proposed_remediation": [
                {"action": "kubectl drain node-01 --ignore-daemonsets"}
            ]
        }
        ok, reason = self._validate(advisory)
        assert ok is False
        assert "ADVISORY_VALIDATION" in reason

    def test_blocks_kubectl_taint(self):
        advisory = {
            "proposed_remediation": [{"action": "kubectl taint nodes node-01 key=value:NoSchedule"}]
        }
        ok, reason = self._validate(advisory)
        assert ok is False

    def test_blocks_rm_rf(self):
        advisory = {
            "proposed_remediation": [{"action": "rm -rf /data/cache"}]
        }
        ok, reason = self._validate(advisory)
        assert ok is False

    def test_blocks_drop_table(self):
        advisory = {
            "proposed_remediation": [{"action": "DROP TABLE users;"}]
        }
        ok, reason = self._validate(advisory)
        assert ok is False

    def test_blocks_delete_from(self):
        advisory = {
            "proposed_remediation": [{"action": "DELETE FROM audit_log WHERE ts < NOW()-7d"}]
        }
        ok, reason = self._validate(advisory)
        assert ok is False

    def test_case_insensitive_matching(self):
        advisory = {
            "proposed_remediation": [{"action": "KUBECTL DELETE pod abc"}]
        }
        ok, _ = self._validate(advisory)
        assert ok is False

    def test_non_dict_steps_skipped(self):
        """Non-dict steps in proposed_remediation do not crash."""
        advisory = {
            "proposed_remediation": ["plain string step", None, 42]
        }
        ok, reason = self._validate(advisory)
        assert ok is True

    def test_empty_proposed_remediation(self):
        ok, reason = self._validate({"proposed_remediation": []})
        assert ok is True

    def test_missing_proposed_remediation_key(self):
        ok, reason = self._validate({})
        assert ok is True

    def test_step_missing_action_key_is_safe(self):
        advisory = {"proposed_remediation": [{"description": "check logs"}]}
        ok, _ = self._validate(advisory)
        assert ok is True


class TestAdvisoryModeKillSwitch_TrapHallucinatedMutation:
    """Tests for trap_hallucinated_mutation (async, uses Redis + Kafka)."""

    @pytest.mark.asyncio
    async def test_trap_writes_audit_block_and_returns_advisory_message(self):
        from workers.advisory_mode_kill_switch import AdvisoryModeKillSwitch

        redis = _make_redis()
        kafka = _KafkaCapture()
        settings = SimpleNamespace(kafka_topic_audit_chain="omni-audit-chain")
        ctx = SimpleNamespace(redis=redis, kafka=kafka, settings=settings)

        result = await AdvisoryModeKillSwitch.trap_hallucinated_mutation(
            tool_name="k8s_rollout_restart",
            args={"deployment": "nginx", "namespace": "production"},
            ctx=ctx,
            trace="trace-abc-123",
            auto_execute_enabled=False,
        )

        assert "ADVISED_ACTION" in result
        assert "k8s_rollout_restart" in result
        assert "trace-abc-123" in result
        # Kafka should have received the audit block
        assert len(kafka.sent) >= 1
        topics = [t for t, _ in kafka.sent]
        assert "omni-audit-chain" in topics

    @pytest.mark.asyncio
    async def test_trap_skips_audit_when_redis_is_none(self):
        """When ctx.redis is None, audit block is skipped — no crash."""
        from workers.advisory_mode_kill_switch import AdvisoryModeKillSwitch

        ctx = SimpleNamespace(redis=None, kafka=None, settings=None)

        result = await AdvisoryModeKillSwitch.trap_hallucinated_mutation(
            tool_name="k8s_scale_deployment",
            args={"replicas": 0},
            ctx=ctx,
            trace="trace-no-redis",
        )
        assert "ADVISED_ACTION" in result

    @pytest.mark.asyncio
    async def test_trap_sends_telegram_when_ctx_has_telegram(self):
        from workers.advisory_mode_kill_switch import AdvisoryModeKillSwitch

        telegram_calls: list[tuple] = []

        class _FakeTelegram:
            async def send_message(self, chat_id, text, parse_mode=None):
                telegram_calls.append((chat_id, text))

        redis = _make_redis()
        kafka = _KafkaCapture()
        settings = SimpleNamespace(kafka_topic_audit_chain="omni-audit-chain")
        ctx = SimpleNamespace(
            redis=redis,
            kafka=kafka,
            settings=settings,
            telegram=_FakeTelegram(),
            _current_chat_id=12345,
        )

        await AdvisoryModeKillSwitch.trap_hallucinated_mutation(
            tool_name="k8s_delete_pod",
            args={"pod": "nginx-abc"},
            ctx=ctx,
            trace="trace-telegram",
        )

        assert len(telegram_calls) == 1
        chat_id, text = telegram_calls[0]
        assert chat_id == 12345
        assert "k8s_delete_pod" in text

    @pytest.mark.asyncio
    async def test_trap_skips_telegram_when_no_chat_id(self):
        """Telegram send is skipped when _current_chat_id is absent."""
        from workers.advisory_mode_kill_switch import AdvisoryModeKillSwitch

        telegram_calls: list[tuple] = []

        class _FakeTelegram:
            async def send_message(self, chat_id, text, parse_mode=None):
                telegram_calls.append((chat_id, text))

        redis = _make_redis()
        kafka = _KafkaCapture()
        settings = SimpleNamespace(kafka_topic_audit_chain="omni-audit-chain")
        ctx = SimpleNamespace(
            redis=redis,
            kafka=kafka,
            settings=settings,
            telegram=_FakeTelegram(),
            # no _current_chat_id
        )

        result = await AdvisoryModeKillSwitch.trap_hallucinated_mutation(
            tool_name="k8s_delete_pod",
            args={},
            ctx=ctx,
            trace="trace-no-chat",
        )

        assert len(telegram_calls) == 0
        assert "ADVISED_ACTION" in result

    @pytest.mark.asyncio
    async def test_trap_returns_audit_chain_failure_on_audit_error(self):
        """When audit chain write fails (AuditLedgerError), function returns AUDIT_CHAIN_FAILURE.

        This exercises lines 116-122 (except AuditLedgerError branch) in kill switch.
        We use a Redis that raises on pipeline.execute to force AuditLedgerError.
        """
        from workers.advisory_mode_kill_switch import AdvisoryModeKillSwitch

        class _BadPipeline:
            """Pipeline stub that always fails on execute."""
            def get(self, *a, **kw):
                return self  # chaining (sync call, not awaitable)

            def incr(self, *a, **kw):
                return self

            async def execute(self):
                raise RuntimeError("redis connection refused")

        class _BadRedis:
            """Redis stub that always fails pipeline.execute."""
            def pipeline(self):
                return _BadPipeline()

        kafka = _KafkaCapture()
        settings = SimpleNamespace(kafka_topic_audit_chain="omni-audit-chain")
        ctx = SimpleNamespace(redis=_BadRedis(), kafka=kafka, settings=settings)

        result = await AdvisoryModeKillSwitch.trap_hallucinated_mutation(
            tool_name="k8s_rollout_restart",
            args={},
            ctx=ctx,
            trace="trace-fail-closed",
        )

        assert "AUDIT_CHAIN_FAILURE" in result
        assert "trace-fail-closed" in result

    @pytest.mark.asyncio
    async def test_trap_handles_telegram_send_error_gracefully(self):
        """Telegram send exception is caught and does NOT propagate.

        This exercises lines 142-143 (except Exception in telegram send).
        """
        from workers.advisory_mode_kill_switch import AdvisoryModeKillSwitch

        class _BrokenTelegram:
            async def send_message(self, chat_id, text, parse_mode=None):
                raise OSError("network error")

        redis = _make_redis()
        kafka = _KafkaCapture()
        settings = SimpleNamespace(kafka_topic_audit_chain="omni-audit-chain")
        ctx = SimpleNamespace(
            redis=redis,
            kafka=kafka,
            settings=settings,
            telegram=_BrokenTelegram(),
            _current_chat_id=999,
        )

        # Must NOT raise; telegram error is swallowed
        result = await AdvisoryModeKillSwitch.trap_hallucinated_mutation(
            tool_name="k8s_delete_pod",
            args={},
            ctx=ctx,
            trace="trace-tg-error",
        )
        assert "ADVISED_ACTION" in result


# ===========================================================================
# 4. adapters/contracts.py
# ===========================================================================

class TestAdapterDataclasses:
    def test_adapter_event_construction(self):
        from workers.adapters.contracts import AdapterEvent
        ev = AdapterEvent(trace_id="t1", source="siem")
        assert ev.trace_id == "t1"
        assert ev.source == "siem"
        assert ev.payload == {}

    def test_adapter_event_with_payload(self):
        from workers.adapters.contracts import AdapterEvent
        ev = AdapterEvent(trace_id="t2", source="prober", payload={"k": "v"})
        assert ev.payload == {"k": "v"}

    def test_adapter_plan_construction(self):
        from workers.adapters.contracts import AdapterPlan
        plan = AdapterPlan(trace_id="t3", tool_name="k8s_rollout_restart")
        assert plan.tool_name == "k8s_rollout_restart"
        assert plan.args == {}
        assert plan.confidence == 0.0

    def test_adapter_plan_with_args(self):
        from workers.adapters.contracts import AdapterPlan
        plan = AdapterPlan(
            trace_id="t4",
            tool_name="k8s_scale_deployment",
            args={"replicas": 3},
            confidence=0.85,
        )
        assert plan.confidence == 0.85
        assert plan.args["replicas"] == 3

    def test_adapter_execution_result_construction(self):
        from workers.adapters.contracts import AdapterExecutionResult
        result = AdapterExecutionResult(trace_id="t5", status="success", exit_code=0)
        assert result.status == "success"
        assert result.exit_code == 0
        assert result.stdout == ""
        assert result.stderr == ""

    def test_adapter_execution_result_with_output(self):
        from workers.adapters.contracts import AdapterExecutionResult
        result = AdapterExecutionResult(
            trace_id="t6",
            status="failure",
            exit_code=1,
            stdout="",
            stderr="command not found",
        )
        assert result.exit_code == 1
        assert result.stderr == "command not found"

    def test_adapter_capability_policy_defaults(self):
        from workers.adapters.contracts import AdapterCapabilityPolicy
        p = AdapterCapabilityPolicy(adapter_name="k8s-prod")
        assert p.allowed_mutators == set()
        assert p.allowed_namespaces == set()
        assert p.require_approval_in_prod is True

    def test_adapter_capability_policy_custom(self):
        from workers.adapters.contracts import AdapterCapabilityPolicy
        p = AdapterCapabilityPolicy(
            adapter_name="k8s-lab",
            allowed_mutators={"k8s_rollout_restart"},
            allowed_namespaces={"multi-agent"},
            require_approval_in_prod=False,
        )
        assert "k8s_rollout_restart" in p.allowed_mutators
        assert "multi-agent" in p.allowed_namespaces
        assert p.require_approval_in_prod is False


class TestPolicyAllowsExecute:
    """Tests for policy_allows_execute function."""

    def _make_policy(self, **kw):
        from workers.adapters.contracts import AdapterCapabilityPolicy
        defaults = dict(adapter_name="test")
        defaults.update(kw)
        return AdapterCapabilityPolicy(**defaults)

    def _check(self, policy, *, env_mode, tool_name, namespace=""):
        from workers.adapters.contracts import policy_allows_execute
        return policy_allows_execute(policy, env_mode=env_mode, tool_name=tool_name, namespace=namespace)

    def test_tool_not_in_allowed_mutators_denied(self):
        policy = self._make_policy(allowed_mutators={"k8s_rollout_restart"})
        ok, reason = self._check(policy, env_mode="lab", tool_name="k8s_delete_pod")
        assert ok is False
        assert reason == "tool_not_allowed_by_adapter_policy"

    def test_tool_allowed_in_lab_env(self):
        policy = self._make_policy(
            allowed_mutators={"k8s_rollout_restart"},
            require_approval_in_prod=True,
        )
        ok, reason = self._check(policy, env_mode="lab", tool_name="k8s_rollout_restart")
        assert ok is True
        assert reason == ""

    def test_tool_blocked_in_prod_when_require_approval(self):
        policy = self._make_policy(
            allowed_mutators={"k8s_rollout_restart"},
            require_approval_in_prod=True,
        )
        ok, reason = self._check(policy, env_mode="prod", tool_name="k8s_rollout_restart")
        assert ok is False
        assert reason == "approval_required_in_prod"

    def test_tool_allowed_in_prod_when_no_approval_required(self):
        policy = self._make_policy(
            allowed_mutators={"k8s_rollout_restart"},
            require_approval_in_prod=False,
        )
        ok, reason = self._check(policy, env_mode="prod", tool_name="k8s_rollout_restart")
        assert ok is True

    def test_namespace_not_in_allowed_denied(self):
        policy = self._make_policy(
            allowed_mutators={"k8s_rollout_restart"},
            allowed_namespaces={"multi-agent"},
            require_approval_in_prod=False,
        )
        ok, reason = self._check(
            policy, env_mode="lab", tool_name="k8s_rollout_restart", namespace="production"
        )
        assert ok is False
        assert reason == "namespace_not_allowed_by_adapter_policy"

    def test_namespace_in_allowed_passes(self):
        policy = self._make_policy(
            allowed_mutators={"k8s_rollout_restart"},
            allowed_namespaces={"multi-agent"},
            require_approval_in_prod=False,
        )
        ok, reason = self._check(
            policy, env_mode="lab", tool_name="k8s_rollout_restart", namespace="multi-agent"
        )
        assert ok is True

    def test_empty_namespace_skips_ns_check(self):
        """If no namespace provided, namespace restriction is not applied."""
        policy = self._make_policy(
            allowed_mutators={"k8s_rollout_restart"},
            allowed_namespaces={"multi-agent"},
            require_approval_in_prod=False,
        )
        ok, reason = self._check(
            policy, env_mode="lab", tool_name="k8s_rollout_restart", namespace=""
        )
        assert ok is True

    def test_empty_allowed_namespaces_skips_ns_check(self):
        """Empty allowed_namespaces set means no namespace restriction."""
        policy = self._make_policy(
            allowed_mutators={"k8s_rollout_restart"},
            allowed_namespaces=set(),
            require_approval_in_prod=False,
        )
        ok, reason = self._check(
            policy, env_mode="lab", tool_name="k8s_rollout_restart", namespace="any-ns"
        )
        assert ok is True

    def test_env_mode_defaults_to_prod_when_empty(self):
        """Empty env_mode treated as prod."""
        policy = self._make_policy(
            allowed_mutators={"k8s_rollout_restart"},
            require_approval_in_prod=True,
        )
        ok, reason = self._check(policy, env_mode="", tool_name="k8s_rollout_restart")
        assert ok is False
        assert reason == "approval_required_in_prod"

    def test_env_mode_case_insensitive(self):
        policy = self._make_policy(
            allowed_mutators={"k8s_rollout_restart"},
            require_approval_in_prod=False,
        )
        ok, reason = self._check(policy, env_mode="LAB", tool_name="k8s_rollout_restart")
        assert ok is True
