"""TDD: advisory_ack — durable Kafka log + operator acknowledgment cho Advisory Mode.

Khác hitl_telegram (mutation-approval): module này không chặn/dispatch mutation nào —
chỉ ghi suggestion + ack vào Kafka/CRAT/DB. Namespace callback riêng ``advack:``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from workers.advisory_ack import (
    ACKNOWLEDGED,
    build_advisory_ack_keyboard,
    emit_advisory_suggestion,
    handle_advisory_ack_callback,
    parse_advisory_ack_callback,
)


class TestBuildAndParseCallback:
    def test_build_keyboard_has_advack_prefix(self):
        kb = build_advisory_ack_keyboard("trace-123")
        cbs = [b["callback_data"] for b in kb["inline_keyboard"][0]]
        assert all(c.startswith("advack:") for c in cbs)
        assert cbs[0] == "advack:ok:trace-123"

    def test_parse_valid_callback(self):
        assert parse_advisory_ack_callback("advack:trace-123") == "trace-123"

    def test_parse_rejects_other_namespaces(self):
        assert parse_advisory_ack_callback("hitl:approve:abc") is None
        assert parse_advisory_ack_callback("ofs:hash:1") is None
        assert parse_advisory_ack_callback("change_approve:xyz") is None

    def test_parse_rejects_empty_trace_id(self):
        assert parse_advisory_ack_callback("advack:") is None


def _ctx(**overrides):
    ctx = MagicMock()
    ctx.settings.kafka_topic_advisory_suggestions = "omni-advisory-suggestions"
    ctx.settings.kafka_topic_audit_chain = "omni-audit-chain"
    ctx.kafka = AsyncMock()
    ctx.redis = AsyncMock()
    ctx.telegram = AsyncMock()
    ctx.admin_repo = AsyncMock()
    ctx.current_tenant_id = "default"
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


@pytest.mark.asyncio
class TestEmitAdvisorySuggestion:
    async def test_emits_pending_ack_to_durable_topic(self):
        ctx = _ctx()
        await emit_advisory_suggestion(
            ctx, trace_id="trace-1", tenant_id="default", advisory_payload={"verdict": "x"},
        )
        ctx.kafka.send_dict.assert_awaited_once()
        topic, msg = ctx.kafka.send_dict.await_args.args
        assert topic == "omni-advisory-suggestions"
        body = json.loads(msg["data"])
        assert body["trace_id"] == "trace-1"
        assert body["status"] == "pending_ack"
        assert body["advisory"] == {"verdict": "x"}

    async def test_no_kafka_does_not_raise(self):
        ctx = _ctx(kafka=None)
        await emit_advisory_suggestion(ctx, trace_id="t", tenant_id="default", advisory_payload={})

    async def test_kafka_error_swallowed_best_effort(self):
        ctx = _ctx()
        ctx.kafka.send_dict.side_effect = RuntimeError("boom")
        await emit_advisory_suggestion(ctx, trace_id="t", tenant_id="default", advisory_payload={})


@pytest.mark.asyncio
class TestHandleAdvisoryAckCallback:
    async def test_ignores_non_callback_update(self):
        ctx = _ctx()
        assert await handle_advisory_ack_callback(ctx, {"message": {}}) is False

    async def test_ignores_other_namespace_callback(self):
        ctx = _ctx()
        update = {"callback_query": {"id": "cq1", "data": "hitl:approve:abc"}}
        assert await handle_advisory_ack_callback(ctx, update) is False

    async def test_acknowledges_writes_crat_kafka_db_and_answers(self, monkeypatch):
        ctx = _ctx()
        write_mock = AsyncMock()
        monkeypatch.setattr("workers.advisory_ack.write_audit_block", write_mock)

        update = {"callback_query": {"id": "cq1", "data": "advack:trace-1", "from": {"id": 555}}}
        handled = await handle_advisory_ack_callback(ctx, update)

        assert handled is True
        write_mock.assert_awaited_once()
        _, kwargs = write_mock.await_args
        assert kwargs["event_type"] == "ADVISORY_DECISION"
        assert kwargs["trace_id"] == "trace-1"
        assert kwargs["payload"]["decision"] == ACKNOWLEDGED
        assert kwargs["payload"]["actor"] == "555"

        ctx.kafka.send_dict.assert_awaited_once()
        topic, msg = ctx.kafka.send_dict.await_args.args
        assert topic == "omni-advisory-suggestions"
        body = json.loads(msg["data"])
        assert body["status"] == "acknowledged"
        assert body["trace_id"] == "trace-1"

        ctx.admin_repo.record_advisory_acknowledgment.assert_awaited_once_with(
            trace_id="trace-1", actor="555", channel="telegram", tenant_id="default",
        )
        ctx.telegram.answer_callback_query.assert_awaited_once()

    async def test_crat_write_failure_blocks_ack_and_alerts_operator(self, monkeypatch):
        from services.audit_ledger.signer import AuditLedgerError

        ctx = _ctx()
        write_mock = AsyncMock(side_effect=AuditLedgerError("sign failed"))
        monkeypatch.setattr("workers.advisory_ack.write_audit_block", write_mock)

        update = {"callback_query": {"id": "cq1", "data": "advack:trace-1", "from": {"id": 555}}}
        handled = await handle_advisory_ack_callback(ctx, update)

        assert handled is True
        ctx.kafka.send_dict.assert_not_awaited()
        ctx.admin_repo.record_advisory_acknowledgment.assert_not_awaited()
        ctx.telegram.answer_callback_query.assert_awaited_once()
        _, kwargs = ctx.telegram.answer_callback_query.await_args
        assert kwargs.get("show_alert") is True

    async def test_missing_admin_repo_does_not_raise(self, monkeypatch):
        ctx = _ctx(admin_repo=None)
        monkeypatch.setattr("workers.advisory_ack.write_audit_block", AsyncMock())
        update = {"callback_query": {"id": "cq1", "data": "advack:trace-1", "from": {"id": 1}}}
        assert await handle_advisory_ack_callback(ctx, update) is True
