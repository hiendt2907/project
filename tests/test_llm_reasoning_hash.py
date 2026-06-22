"""Tests for S1.4 — LLM Reasoning Hash in CRAT ADVISORY_DECISION."""

from __future__ import annotations

import hashlib
import json
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call


class TestAdvisoryAnalystLLMHash:
    """Verify that advisory_analyst_handler stores LLM hash and enriches CRAT payload."""

    def _make_ctx(self, redis_mock):
        ws = SimpleNamespace(
            kafka_topic_audit_chain="omni-audit-chain",
            diag_evidence_llm_model="qwen3.6",
            llm_chat_timeout_sec=120,
            omni_llm_trace_enabled=False,
        )
        return SimpleNamespace(
            redis=redis_mock,
            kafka=AsyncMock(),
            settings=ws,
            llm=AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_llm_hash_stored_in_redis(self):
        """After successful LLM parse, raw response must be stored in Redis."""
        redis = AsyncMock()
        ctx = self._make_ctx(redis)

        raw_llm = '{"verdict": "CRITICAL", "root_cause": "OOM", "trace_id": "t1"}'
        expected_hash = hashlib.sha256(raw_llm.encode()).hexdigest()

        mock_advisory = MagicMock()
        mock_advisory.model_dump.return_value = {"verdict": "CRITICAL", "root_cause": "OOM"}
        mock_advisory.model_copy.return_value = mock_advisory  # _compute_escalation_tier calls model_copy

        with (
            patch("workers.advisory_analyst_handler._parse_advisory_json", return_value={"verdict": "CRITICAL", "root_cause": "OOM"}),
            patch("workers.advisory_analyst_handler.AnalystAdvisory", return_value=mock_advisory),
            patch("workers.advisory_analyst_handler._correct_escalation_reason", return_value=mock_advisory),
            patch("workers.advisory_analyst_handler.log_llm_trace"),
            patch("workers.advisory_analyst_handler.log_start_request_ctx"),
            patch("workers.advisory_analyst_handler.log_end_request_ctx"),
            patch("workers.advisory_analyst_handler.inc_llm_requests"),
            patch("workers.advisory_analyst_handler.write_audit_block", new=AsyncMock()) as mock_audit,
        ):
            ctx.llm.chat = AsyncMock(return_value={"message": {"content": raw_llm}})

            from workers.advisory_analyst_handler import run_advisory_analyst
            await run_advisory_analyst(ctx, payload={}, trace="t1", evidence_text="test evidence")

        # Redis setex should have been called with the LLM reason key and raw content
        setex_calls = redis.setex.call_args_list
        llm_reason_calls = [c for c in setex_calls if "omni:crat:llm_reason:t1:advisory" in str(c)]
        assert len(llm_reason_calls) == 1
        stored_content = llm_reason_calls[0][0][2]
        assert stored_content == raw_llm

        # The hash in the audit payload should match
        audit_call = mock_audit.call_args
        payload = audit_call[1]["payload"]
        assert payload["llm_reasoning_hash"] == expected_hash
        assert payload["llm_reasoning_ref"] == "omni:crat:llm_reason:t1:advisory"

    @pytest.mark.asyncio
    async def test_redis_failure_does_not_abort_advisory(self):
        """Redis setex failure must not prevent advisory from being returned."""
        redis = AsyncMock()
        redis.setex.side_effect = Exception("redis down")
        ctx = self._make_ctx(redis)

        raw_llm = '{"verdict": "OK", "root_cause": "none", "trace_id": "t2"}'
        mock_advisory = MagicMock()
        mock_advisory.model_dump.return_value = {"verdict": "OK"}
        mock_advisory.model_dump_json.return_value = '{"verdict": "OK"}'

        with (
            patch("workers.advisory_analyst_handler._parse_advisory_json", return_value={"verdict": "OK"}),
            patch("workers.advisory_analyst_handler.AnalystAdvisory", return_value=mock_advisory),
            patch("workers.advisory_analyst_handler._correct_escalation_reason", return_value=mock_advisory),
            patch("workers.advisory_analyst_handler.log_llm_trace"),
            patch("workers.advisory_analyst_handler.log_start_request_ctx"),
            patch("workers.advisory_analyst_handler.log_end_request_ctx"),
            patch("workers.advisory_analyst_handler.inc_llm_requests"),
            patch("workers.advisory_analyst_handler.write_audit_block", new=AsyncMock()),
        ):
            ctx.llm.chat = AsyncMock(return_value={"message": {"content": raw_llm}})

            from workers.advisory_analyst_handler import run_advisory_analyst
            result = await run_advisory_analyst(ctx, payload={}, trace="t2", evidence_text="test")

        # Advisory must still be returned even when Redis fails
        assert result is not None


class TestReplanLLMHash:
    """Verify that _llm_replan_after_feedback stores LLM hash in Redis."""

    def _make_ctx(self, redis_mock):
        ws = SimpleNamespace(
            diag_evidence_llm_model="qwen3.6",
            chat_model="qwen3.6",
            omni_llm_trace_enabled=False,
        )
        return SimpleNamespace(
            redis=redis_mock,
            kafka=AsyncMock(),
            settings=ws,
            llm=AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_replan_stores_raw_llm_in_redis(self):
        redis = AsyncMock()
        redis.get.return_value = None  # no ctx blob
        ctx = self._make_ctx(redis)

        raw_content = '{"tool_name": "k8s_rollout_restart", "args": {"namespace": "ns", "deployment": "dep"}}'
        ctx.llm.chat = AsyncMock(return_value={"message": {"content": raw_content}})

        with patch("workers.advisory_analyst_handler.log_llm_trace"):
            from workers.autonomous_feedback_loop import _llm_replan_after_feedback
            result = await _llm_replan_after_feedback(ctx, "trace-r1", "stdout", "", 1)

        assert result is not None
        assert result["tool_name"] == "k8s_rollout_restart"

        # Redis setex should have stored raw LLM content
        setex_calls = redis.setex.call_args_list
        llm_calls = [c for c in setex_calls if "omni:crat:llm_reason:trace-r1:replan" in str(c)]
        assert len(llm_calls) == 1
        assert llm_calls[0][0][2] == raw_content

    @pytest.mark.asyncio
    async def test_replan_redis_failure_returns_plan_anyway(self):
        redis = AsyncMock()
        redis.get.return_value = None
        redis.setex.side_effect = Exception("redis down")
        ctx = self._make_ctx(redis)

        raw_content = '{"tool_name": "k8s_rollout_restart", "args": {"namespace": "ns", "deployment": "dep"}}'
        ctx.llm.chat = AsyncMock(return_value={"message": {"content": raw_content}})

        from workers.autonomous_feedback_loop import _llm_replan_after_feedback
        result = await _llm_replan_after_feedback(ctx, "trace-r2", "stdout", "", 1)
        # Must return the plan even if Redis fails
        assert result is not None
