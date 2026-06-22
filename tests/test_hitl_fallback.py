"""Tests for S1.3 — HITL Fallback Channel + Dead-Letter Queue."""

from __future__ import annotations

import json
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from workers.hitl_fallback import emit_slack_fallback, store_dead_letter


class TestEmitSlackFallback:
    @pytest.mark.asyncio
    async def test_empty_webhook_url_returns_false(self):
        client = AsyncMock()
        ok = await emit_slack_fallback(
            client=client,
            webhook_url="",
            trace_id="t1",
            tool_name="k8s_scale_deployment",
            incident_id="inc-001",
            explain="memory OOM",
            elapsed_sec=900,
        )
        assert ok is False
        client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_slack_post(self):
        client = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        client.post.return_value = resp

        ok = await emit_slack_fallback(
            client=client,
            webhook_url="https://hooks.slack.com/services/fake",
            trace_id="t2",
            tool_name="k8s_patch_configmap",
            incident_id="inc-002",
            explain="configmap key missing",
            elapsed_sec=901,
        )
        assert ok is True
        client.post.assert_called_once()
        call_kwargs = client.post.call_args
        assert call_kwargs[0][0] == "https://hooks.slack.com/services/fake"
        body = json.loads(call_kwargs[1]["content"])
        assert "t2" in body["text"]
        assert "901" in body["text"]

    @pytest.mark.asyncio
    async def test_slack_non_200_returns_false(self):
        client = AsyncMock()
        resp = MagicMock()
        resp.status_code = 403
        resp.text = "invalid_token"
        client.post.return_value = resp

        ok = await emit_slack_fallback(
            client=client,
            webhook_url="https://hooks.slack.com/services/fake",
            trace_id="t3",
            tool_name="k8s_scale_deployment",
            incident_id="inc-003",
            explain="",
            elapsed_sec=910,
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_slack_exception_returns_false(self):
        client = AsyncMock()
        client.post.side_effect = Exception("network error")

        ok = await emit_slack_fallback(
            client=client,
            webhook_url="https://hooks.slack.com/services/fake",
            trace_id="t4",
            tool_name="k8s_scale_deployment",
            incident_id="inc-004",
            explain="test",
            elapsed_sec=900,
        )
        assert ok is False  # must not raise


class TestStoreDeadLetter:
    @pytest.mark.asyncio
    async def test_stores_correct_fields_in_redis(self):
        redis = AsyncMock()
        await store_dead_letter(
            redis=redis,
            trace_id="trace-timeout-001",
            incident_id="inc-099",
            tool_name="k8s_scale_deployment",
            raw_body={"action": "execute_mutate", "tool_name": "k8s_scale_deployment"},
            reason="HITL_TIMEOUT after 1800s",
        )

        redis.hset.assert_called_once()
        call_kwargs = redis.hset.call_args[1]
        mapping = call_kwargs["mapping"]
        assert mapping["trace_id"] == "trace-timeout-001"
        assert mapping["tool_name"] == "k8s_scale_deployment"
        assert "HITL_TIMEOUT" in mapping["reason"]
        assert "execute_mutate" in mapping["action_json"]
        redis.expire.assert_called_once_with("omni:hitl:deadletter:inc-099", 86400)

    @pytest.mark.asyncio
    async def test_none_redis_does_not_raise(self):
        # Should be a no-op when redis is None
        await store_dead_letter(
            redis=None,
            trace_id="t",
            incident_id="inc",
            tool_name="tool",
            raw_body={},
            reason="test",
        )

    @pytest.mark.asyncio
    async def test_redis_exception_does_not_raise(self):
        redis = AsyncMock()
        redis.hset.side_effect = Exception("redis down")

        await store_dead_letter(
            redis=redis,
            trace_id="t",
            incident_id="inc",
            tool_name="tool",
            raw_body={},
            reason="test",
        )
        # Must not raise — storage failure is logged but not propagated

    @pytest.mark.asyncio
    async def test_custom_ttl_passed_to_expire(self):
        redis = AsyncMock()
        await store_dead_letter(
            redis=redis,
            trace_id="t",
            incident_id="inc-custom",
            tool_name="tool",
            raw_body={},
            reason="test",
            ttl=7200,
        )
        redis.expire.assert_called_once_with("omni:hitl:deadletter:inc-custom", 7200)
