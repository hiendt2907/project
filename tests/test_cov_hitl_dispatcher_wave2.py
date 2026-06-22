"""
Wave-2 coverage tests for workers.hitl_dispatcher.
Targets uncovered lines: _parse_pending edge cases, _register_pending,
_poll_api_for_decision, _emit, _set_audit_state, and _process flow paths.
"""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_ENV_MODE", "dev")

from workers.hitl_dispatcher import (
    _Decision,
    _ParseError,
    _PendingAction,
    _build_rejection_feedback,
    _jitter_sleep_sec,
    _parse_pending,
    _poll_api_for_decision,
    _register_pending,
    _set_audit_state,
    _emit,
    _process,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_kafka_bytes(body: dict) -> bytes:
    return json.dumps({"data": json.dumps(body)}).encode()


def _make_action(**overrides) -> _PendingAction:
    base = dict(
        trace_id="trace-001",
        tool_name="k8s_delete_pod",
        args={"pod": "bad-pod"},
        reasoning_chain=None,
        siem_incident_id="inc-001",
        siem_tenant="acme",
        siem_category="ddos",
        siem_playbook_id="pb-1",
        explain="High traffic detected",
        advise="Delete the rogue pod",
        hitl_reason="siem_critical_action",
        raw_body={"action": "execute_mutate"},
    )
    base.update(overrides)
    return _PendingAction(**base)


# ---------------------------------------------------------------------------
# _parse_pending edge cases
# ---------------------------------------------------------------------------

class TestParsePending:
    def test_invalid_outer_json_raises_parse_error(self):
        with pytest.raises(_ParseError, match="outer json decode"):
            _parse_pending(b"not-json{{")

    def test_inner_data_invalid_json_raises_parse_error(self):
        raw = json.dumps({"data": "not-json{{"}).encode()
        with pytest.raises(_ParseError, match="inner data json decode"):
            _parse_pending(raw)

    def test_body_not_dict_raises_parse_error(self):
        raw = json.dumps({"data": json.dumps([1, 2, 3])}).encode()
        with pytest.raises(_ParseError, match="body must be a JSON object"):
            _parse_pending(raw)

    def test_missing_trace_id_raises_parse_error(self):
        raw = _make_kafka_bytes({"tool_name": "delete_pod"})
        with pytest.raises(_ParseError, match="missing trace_id"):
            _parse_pending(raw)

    def test_uses_id_field_as_trace_fallback(self):
        raw = _make_kafka_bytes({"id": "fallback-id", "tool_name": "delete_pod"})
        action = _parse_pending(raw)
        assert action.trace_id == "fallback-id"

    def test_args_non_dict_defaults_to_empty(self):
        raw = _make_kafka_bytes({"trace_id": "t1", "args": "bad-args"})
        action = _parse_pending(raw)
        assert action.args == {}

    def test_reasoning_chain_non_dict_defaults_to_none(self):
        raw = _make_kafka_bytes({"trace_id": "t1", "reasoning_chain": "should be dict"})
        action = _parse_pending(raw)
        assert action.reasoning_chain is None

    def test_reasoning_chain_dict_preserved(self):
        raw = _make_kafka_bytes({"trace_id": "t1", "reasoning_chain": {"step": 1}})
        action = _parse_pending(raw)
        assert action.reasoning_chain == {"step": 1}

    def test_explain_truncated_to_500_chars(self):
        long_explain = "A" * 600
        raw = _make_kafka_bytes({"trace_id": "t1", "explain": long_explain})
        action = _parse_pending(raw)
        assert len(action.explain) == 500

    def test_siem_fields_populated(self):
        raw = _make_kafka_bytes({
            "trace_id": "t1",
            "siem_incident_id": "inc-99",
            "siem_tenant": "tenant-x",
            "siem_category": "malware",
            "siem_playbook_id": "pb-99",
        })
        action = _parse_pending(raw)
        assert action.siem_incident_id == "inc-99"
        assert action.siem_tenant == "tenant-x"
        assert action.siem_category == "malware"
        assert action.siem_playbook_id == "pb-99"

    def test_siem_incident_falls_back_to_trace(self):
        raw = _make_kafka_bytes({"trace_id": "t1"})
        action = _parse_pending(raw)
        assert action.siem_incident_id == "t1"

    def test_data_as_dict_not_string(self):
        """data field can also be a dict (not string)."""
        outer = {"data": {"trace_id": "t1", "tool_name": "some_tool"}}
        raw = json.dumps(outer).encode()
        action = _parse_pending(raw)
        assert action.trace_id == "t1"

    def test_hitl_fields_stripped_from_raw_body(self):
        raw = _make_kafka_bytes({
            "trace_id": "t1",
            "hitl_pending": True,
            "hitl_reason": "r1",
            "siem_incident_id": "inc1",
            "siem_tenant": "t",
            "siem_category": "c",
            "siem_playbook_id": "pb",
            "explain": "ex",
            "advise": "adv",
            "tool_name": "delete_pod",
        })
        action = _parse_pending(raw)
        for key in ("hitl_pending", "hitl_reason", "siem_incident_id", "siem_tenant",
                    "siem_category", "siem_playbook_id", "explain", "advise"):
            assert key not in action.raw_body


# ---------------------------------------------------------------------------
# _jitter_sleep_sec
# ---------------------------------------------------------------------------

class TestJitterSleep:
    def test_returns_zero_at_attempt_zero_with_tiny_base(self):
        # sleep = random(0, min(cap, base * 2^0)) = random(0, base)
        # Should be >= 0 and <= base
        val = _jitter_sleep_sec(0, 0.0)
        assert val == 0.0

    def test_respects_cap(self):
        for _ in range(10):
            val = _jitter_sleep_sec(100, 1.0, cap=5.0)
            assert 0.0 <= val <= 5.0

    def test_grows_with_attempt(self):
        # At attempt=10 with base=1, 2^10=1024 exceeds cap=60 → result bounded at 60.
        val = _jitter_sleep_sec(10, 1.0, cap=60.0)
        assert 0.0 <= val <= 60.0


# ---------------------------------------------------------------------------
# _emit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emit_encodes_and_sends():
    producer = AsyncMock()
    producer.send_and_wait = AsyncMock()

    await _emit(producer, "test-topic", {"foo": "bar"})

    producer.send_and_wait.assert_called_once()
    call_args = producer.send_and_wait.call_args
    assert call_args[0][0] == "test-topic"
    payload = json.loads(call_args[1]["value"].decode())
    inner = json.loads(payload["data"])
    assert inner["foo"] == "bar"


# ---------------------------------------------------------------------------
# _set_audit_state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_audit_state_writes_correct_key():
    import fakeredis.aioredis
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await _set_audit_state(redis, "trace-xyz", "APPROVED", "delete_pod", ttl=3600)
    val = await redis.get("omni:hitl:state:trace-xyz")
    assert val is not None
    data = json.loads(val)
    assert data["status"] == "APPROVED"
    assert data["tool_name"] == "delete_pod"


@pytest.mark.asyncio
async def test_set_audit_state_swallows_redis_errors(caplog):
    redis = AsyncMock()
    redis.setex = AsyncMock(side_effect=Exception("Redis down"))
    # Should not raise
    await _set_audit_state(redis, "t1", "PENDING", "tool", ttl=60)


# ---------------------------------------------------------------------------
# _register_pending
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_pending_no_token_returns_false():
    action = _make_action()
    async with httpx.AsyncClient() as client:
        with patch("workers.hitl_dispatcher._HITL_API_TOKEN", ""):
            result = await _register_pending(client, action)
    assert result is False


@pytest.mark.asyncio
async def test_register_pending_201_returns_true():
    action = _make_action()
    mock_resp = MagicMock()
    mock_resp.status_code = 201

    async with httpx.AsyncClient() as client:
        with (
            patch("workers.hitl_dispatcher._HITL_API_TOKEN", "secret"),
            patch.object(client, "post", new=AsyncMock(return_value=mock_resp)),
        ):
            result = await _register_pending(client, action)
    assert result is True


@pytest.mark.asyncio
async def test_register_pending_4xx_returns_false_no_retry():
    action = _make_action()
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.text = "Forbidden"
    call_count = 0

    async def _fake_post(*a, **kw):
        nonlocal call_count
        call_count += 1
        return mock_resp

    async with httpx.AsyncClient() as client:
        with (
            patch("workers.hitl_dispatcher._HITL_API_TOKEN", "secret"),
            patch.object(client, "post", new=_fake_post),
            patch("workers.hitl_dispatcher._REGISTER_MAX_RETRIES", 3),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            result = await _register_pending(client, action)

    assert result is False
    assert call_count == 1  # no retry on 4xx


@pytest.mark.asyncio
async def test_register_pending_5xx_retries_exhausted_returns_false():
    action = _make_action()
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_resp.text = "Service Unavailable"

    async with httpx.AsyncClient() as client:
        with (
            patch("workers.hitl_dispatcher._HITL_API_TOKEN", "secret"),
            patch.object(client, "post", new=AsyncMock(return_value=mock_resp)),
            patch("workers.hitl_dispatcher._REGISTER_MAX_RETRIES", 2),
            patch("workers.hitl_dispatcher._jitter_sleep_sec", return_value=0.0),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            result = await _register_pending(client, action)

    assert result is False


@pytest.mark.asyncio
async def test_register_pending_network_error_retries():
    action = _make_action()
    attempts = []

    async def _fake_post(*a, **kw):
        attempts.append(1)
        raise httpx.ConnectError("Connection refused")

    async with httpx.AsyncClient() as client:
        with (
            patch("workers.hitl_dispatcher._HITL_API_TOKEN", "secret"),
            patch.object(client, "post", new=_fake_post),
            patch("workers.hitl_dispatcher._REGISTER_MAX_RETRIES", 3),
            patch("workers.hitl_dispatcher._jitter_sleep_sec", return_value=0.0),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            result = await _register_pending(client, action)

    assert result is False
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_register_pending_explain_and_advise_in_reason():
    action = _make_action(explain="High traffic", advise="Delete pod")
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    captured_payload = {}

    async def _fake_post(url, *, json, headers, timeout):
        captured_payload.update(json)
        return mock_resp

    async with httpx.AsyncClient() as client:
        with (
            patch("workers.hitl_dispatcher._HITL_API_TOKEN", "tok"),
            patch.object(client, "post", new=_fake_post),
        ):
            await _register_pending(client, action)

    assert "High traffic" in captured_payload["reason"]
    assert "Delete pod" in captured_payload["reason"]


# ---------------------------------------------------------------------------
# _poll_api_for_decision
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_returns_approved():
    action = _make_action()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "approved"}

    async with httpx.AsyncClient() as client:
        with (
            patch("workers.hitl_dispatcher._HITL_API_TOKEN", "tok"),
            patch("workers.hitl_dispatcher._APPROVAL_TIMEOUT_SEC", 60),
            patch("workers.hitl_dispatcher._POLL_INTERVAL_BASE_SEC", 0),
            patch.object(client, "get", new=AsyncMock(return_value=mock_resp)),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            decision = await _poll_api_for_decision(client, action)

    assert decision == _Decision.APPROVED


@pytest.mark.asyncio
async def test_poll_returns_rejected():
    action = _make_action()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "rejected"}

    async with httpx.AsyncClient() as client:
        with (
            patch("workers.hitl_dispatcher._HITL_API_TOKEN", "tok"),
            patch("workers.hitl_dispatcher._APPROVAL_TIMEOUT_SEC", 60),
            patch("workers.hitl_dispatcher._POLL_INTERVAL_BASE_SEC", 0),
            patch.object(client, "get", new=AsyncMock(return_value=mock_resp)),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            decision = await _poll_api_for_decision(client, action)

    assert decision == _Decision.REJECTED


@pytest.mark.asyncio
async def test_poll_returns_timeout_when_deadline_expired():
    action = _make_action()

    import time as _time_mod

    call_count = 0
    original_monotonic = _time_mod.monotonic

    def _fake_monotonic():
        nonlocal call_count
        call_count += 1
        # First call: deadline set to now+1s; subsequent calls advance time so deadline passes.
        if call_count == 1:
            return original_monotonic()
        return original_monotonic() + 9999

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "pending"}

    async with httpx.AsyncClient() as client:
        with (
            patch("workers.hitl_dispatcher._HITL_API_TOKEN", "tok"),
            patch("workers.hitl_dispatcher._APPROVAL_TIMEOUT_SEC", 1),
            patch("workers.hitl_dispatcher._POLL_INTERVAL_BASE_SEC", 0),
            patch("workers.hitl_dispatcher.time") as mock_time,
            patch.object(client, "get", new=AsyncMock(return_value=mock_resp)),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            # First call sets deadline, second call after while-condition check sees expiry
            mock_time.monotonic.side_effect = [
                _time_mod.monotonic(),         # deadline = now + 1
                _time_mod.monotonic() + 9999,  # while condition: now > deadline → exit
            ]
            decision = await _poll_api_for_decision(client, action)

    assert decision == _Decision.TIMEOUT


@pytest.mark.asyncio
async def test_poll_404_continues_polling():
    action = _make_action()
    responses = [
        MagicMock(status_code=404),
        MagicMock(status_code=200, json=MagicMock(return_value={"status": "approved"})),
    ]

    async with httpx.AsyncClient() as client:
        with (
            patch("workers.hitl_dispatcher._HITL_API_TOKEN", "tok"),
            patch("workers.hitl_dispatcher._APPROVAL_TIMEOUT_SEC", 60),
            patch("workers.hitl_dispatcher._POLL_INTERVAL_BASE_SEC", 0),
            patch.object(client, "get", new=AsyncMock(side_effect=responses)),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            decision = await _poll_api_for_decision(client, action)

    assert decision == _Decision.APPROVED


@pytest.mark.asyncio
async def test_poll_network_error_uses_backoff():
    action = _make_action()

    responses = [
        httpx.ConnectError("refused"),
        MagicMock(status_code=200, json=MagicMock(return_value={"status": "rejected"})),
    ]

    sleep_calls = []

    async def _fake_sleep(s):
        sleep_calls.append(s)

    async with httpx.AsyncClient() as client:
        with (
            patch("workers.hitl_dispatcher._HITL_API_TOKEN", "tok"),
            patch("workers.hitl_dispatcher._APPROVAL_TIMEOUT_SEC", 60),
            patch("workers.hitl_dispatcher._POLL_INTERVAL_BASE_SEC", 10),
            patch("workers.hitl_dispatcher._jitter_sleep_sec", return_value=5.0),
            patch.object(client, "get", new=AsyncMock(side_effect=responses)),
            patch("asyncio.sleep", new=AsyncMock(side_effect=_fake_sleep)),
        ):
            decision = await _poll_api_for_decision(client, action)

    assert decision == _Decision.REJECTED
    # First sleep used backoff (5.0), second used base interval (10)
    assert sleep_calls[0] == 5.0


@pytest.mark.asyncio
async def test_poll_bad_json_response_continues():
    action = _make_action()

    def _bad_json():
        raise ValueError("no json")

    responses = [
        MagicMock(status_code=200, json=_bad_json),
        MagicMock(status_code=200, json=MagicMock(return_value={"status": "approved"})),
    ]

    async with httpx.AsyncClient() as client:
        with (
            patch("workers.hitl_dispatcher._HITL_API_TOKEN", "tok"),
            patch("workers.hitl_dispatcher._APPROVAL_TIMEOUT_SEC", 60),
            patch("workers.hitl_dispatcher._POLL_INTERVAL_BASE_SEC", 0),
            patch.object(client, "get", new=AsyncMock(side_effect=responses)),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            decision = await _poll_api_for_decision(client, action)

    assert decision == _Decision.APPROVED


@pytest.mark.asyncio
async def test_poll_5xx_increments_consecutive_errors():
    action = _make_action()

    responses = [
        MagicMock(status_code=503, json=MagicMock(return_value={})),
        MagicMock(status_code=200, json=MagicMock(return_value={"status": "approved"})),
    ]

    sleep_calls = []

    async def _fake_sleep(s):
        sleep_calls.append(s)

    async with httpx.AsyncClient() as client:
        with (
            patch("workers.hitl_dispatcher._HITL_API_TOKEN", "tok"),
            patch("workers.hitl_dispatcher._APPROVAL_TIMEOUT_SEC", 60),
            patch("workers.hitl_dispatcher._POLL_INTERVAL_BASE_SEC", 5),
            patch("workers.hitl_dispatcher._jitter_sleep_sec", return_value=3.0),
            patch.object(client, "get", new=AsyncMock(side_effect=responses)),
            patch("asyncio.sleep", new=AsyncMock(side_effect=_fake_sleep)),
        ):
            decision = await _poll_api_for_decision(client, action)

    assert decision == _Decision.APPROVED
    assert sleep_calls[0] == 3.0  # backoff applied after 5xx


# ---------------------------------------------------------------------------
# _process — approved, rejected, timeout, register-failed paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_approved_emits_to_actions():
    import fakeredis.aioredis
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    producer = AsyncMock()
    producer.send_and_wait = AsyncMock()
    action = _make_action(raw_body={"action": "execute_mutate", "trace_id": "trace-001"})

    with (
        patch("workers.hitl_dispatcher._HITL_API_TOKEN", "tok"),
        patch("workers.hitl_dispatcher._register_pending", new=AsyncMock(return_value=True)),
        patch("workers.hitl_dispatcher._poll_api_for_decision", new=AsyncMock(return_value=_Decision.APPROVED)),
        patch("workers.hitl_dispatcher._TOPIC_ACTIONS", "omni-actions"),
    ):
        await _process(action, producer, redis, MagicMock())

    calls = producer.send_and_wait.call_args_list
    topics = [c[0][0] for c in calls]
    assert "omni-actions" in topics


@pytest.mark.asyncio
async def test_process_rejected_emits_feedback():
    import fakeredis.aioredis
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    producer = AsyncMock()
    producer.send_and_wait = AsyncMock()
    action = _make_action()

    with (
        patch("workers.hitl_dispatcher._HITL_API_TOKEN", "tok"),
        patch("workers.hitl_dispatcher._register_pending", new=AsyncMock(return_value=True)),
        patch("workers.hitl_dispatcher._poll_api_for_decision", new=AsyncMock(return_value=_Decision.REJECTED)),
        patch("workers.hitl_dispatcher._TOPIC_FEEDBACK", "omni-action-feedback"),
    ):
        await _process(action, producer, redis, MagicMock())

    calls = producer.send_and_wait.call_args_list
    topics = [c[0][0] for c in calls]
    assert "omni-action-feedback" in topics


@pytest.mark.asyncio
async def test_process_timeout_emits_feedback():
    import fakeredis.aioredis
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    producer = AsyncMock()
    producer.send_and_wait = AsyncMock()
    action = _make_action()

    with (
        patch("workers.hitl_dispatcher._HITL_API_TOKEN", "tok"),
        patch("workers.hitl_dispatcher._register_pending", new=AsyncMock(return_value=True)),
        patch("workers.hitl_dispatcher._poll_api_for_decision", new=AsyncMock(return_value=_Decision.TIMEOUT)),
        patch("workers.hitl_dispatcher._TOPIC_FEEDBACK", "omni-action-feedback"),
    ):
        await _process(action, producer, redis, MagicMock())

    calls = producer.send_and_wait.call_args_list
    topics = [c[0][0] for c in calls]
    assert "omni-action-feedback" in topics


@pytest.mark.asyncio
async def test_process_register_failed_auto_rejects():
    import fakeredis.aioredis
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    producer = AsyncMock()
    producer.send_and_wait = AsyncMock()
    action = _make_action()

    poll_called = False

    async def _fake_poll(*a, **kw):
        nonlocal poll_called
        poll_called = True
        return _Decision.APPROVED

    with (
        patch("workers.hitl_dispatcher._HITL_API_TOKEN", "tok"),
        patch("workers.hitl_dispatcher._register_pending", new=AsyncMock(return_value=False)),
        patch("workers.hitl_dispatcher._poll_api_for_decision", new=_fake_poll),
        patch("workers.hitl_dispatcher._TOPIC_FEEDBACK", "omni-action-feedback"),
    ):
        await _process(action, producer, redis, MagicMock())

    # Poll should NOT be called when registration fails
    assert not poll_called
    calls = producer.send_and_wait.call_args_list
    topics = [c[0][0] for c in calls]
    assert "omni-action-feedback" in topics


@pytest.mark.asyncio
async def test_process_sets_audit_state_awaiting_hitl():
    import fakeredis.aioredis
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    redis_str = fakeredis.aioredis.FakeRedis(decode_responses=True)
    producer = AsyncMock()
    producer.send_and_wait = AsyncMock()
    action = _make_action(trace_id="audit-trace-1")

    with (
        patch("workers.hitl_dispatcher._HITL_API_TOKEN", "tok"),
        patch("workers.hitl_dispatcher._register_pending", new=AsyncMock(return_value=True)),
        patch("workers.hitl_dispatcher._poll_api_for_decision", new=AsyncMock(return_value=_Decision.APPROVED)),
    ):
        await _process(action, producer, redis_str, MagicMock())

    val = await redis_str.get("omni:hitl:state:audit-trace-1")
    # State should have been written (APPROVED_FORWARDED)
    assert val is not None
    data = json.loads(val)
    assert data["status"] == "APPROVED_FORWARDED"
