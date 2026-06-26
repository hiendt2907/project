"""Tests for knowledge pipeline — signal routing, confidence score, change detection."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis

from anomaly.remote_host_baseline import (
    ConfidenceLevel,
    score_to_level,
    add_confidence,
    get_confidence_score,
    decay_confidence,
)
from remote_agent.discovery import diff_discovery
from workers.knowledge_pipeline import (
    handle_knowledge_evidence,
    handle_telegram_doc_upload,
    _LOG_STORE_MAX,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def redis():
    return FakeRedis(decode_responses=True)


def _ctx(redis_client):
    return SimpleNamespace(
        redis=redis_client,
        telegram=None,
        telegram_chat_id=None,
        kafka=None,
        settings=SimpleNamespace(),
        ledger=SimpleNamespace(record_exception=AsyncMock()),
    )


# ---------------------------------------------------------------------------
# Phase 1: signal_type routing (INV_KNOWLEDGE_NOT_ALERT)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_signal_type_log_sample_stored(redis):
    ctx = _ctx(redis)
    ev = {
        "signal_type": "LOG_SAMPLE",
        "tenant_id": "t1",
        "extracted_fact": {"agent_id": "agent-001"},
        "ts": "1700000000",
        "alert_hint": "",
        "raw": "2024-01-01 info ok",
    }
    await handle_knowledge_evidence(ctx, ev)
    key = "omni:knowledge:logs:agent-001:rolling"
    entries = await redis.lrange(key, 0, -1)
    assert len(entries) == 1
    record = json.loads(entries[0])
    assert "ts" in record


@pytest.mark.asyncio
async def test_log_sample_rolling_trim(redis):
    ctx = _ctx(redis)
    agent_id = "agent-trim"
    for i in range(_LOG_STORE_MAX + 5):
        ev = {
            "signal_type": "LOG_SAMPLE",
            "tenant_id": "t1",
            "extracted_fact": {"agent_id": agent_id},
            "ts": str(1700000000 + i),
            "alert_hint": "",
            "raw": f"line {i}",
        }
        await handle_knowledge_evidence(ctx, ev)
    key = f"omni:knowledge:logs:{agent_id}:rolling"
    length = await redis.llen(key)
    assert length == _LOG_STORE_MAX


@pytest.mark.asyncio
async def test_unknown_signal_type_noop(redis):
    ctx = _ctx(redis)
    ev = {"signal_type": "BOGUS", "tenant_id": "t1", "extracted_fact": {}}
    # Should not raise
    await handle_knowledge_evidence(ctx, ev)


# ---------------------------------------------------------------------------
# Phase 3: Confidence Score
# ---------------------------------------------------------------------------

def test_score_to_level():
    assert score_to_level(0) == ConfidenceLevel.STATIC_GUARD
    assert score_to_level(24) == ConfidenceLevel.STATIC_GUARD
    assert score_to_level(25) == ConfidenceLevel.LEARNING
    assert score_to_level(49) == ConfidenceLevel.LEARNING
    assert score_to_level(50) == ConfidenceLevel.ASSISTED
    assert score_to_level(74) == ConfidenceLevel.ASSISTED
    assert score_to_level(75) == ConfidenceLevel.AUTONOMOUS
    assert score_to_level(100) == ConfidenceLevel.AUTONOMOUS


@pytest.mark.asyncio
async def test_add_confidence_clamps_to_100(redis):
    await add_confidence(redis, tenant_id="t1", host="h1", delta=90)
    await add_confidence(redis, tenant_id="t1", host="h1", delta=90)
    score = await get_confidence_score(redis, tenant_id="t1", host="h1")
    assert score == 100


@pytest.mark.asyncio
async def test_add_confidence_floor_at_zero(redis):
    score = await add_confidence(redis, tenant_id="t1", host="h1", delta=-50)
    assert score == 0


@pytest.mark.asyncio
async def test_decay_confidence(redis):
    await add_confidence(redis, tenant_id="t1", host="h1", delta=30)
    after = await decay_confidence(redis, tenant_id="t1", host="h1", decay=5)
    assert after == 25


@pytest.mark.asyncio
async def test_add_confidence_notify_on_level_change(redis):
    called: list[tuple] = []

    async def notify(old_level, new_level, tid, host):
        called.append((old_level, new_level))

    # Start at 20 (STATIC_GUARD), bump to 30 (LEARNING)
    await add_confidence(redis, tenant_id="t2", host="h2", delta=20)
    await add_confidence(redis, tenant_id="t2", host="h2", delta=10, notify_fn=notify)
    assert len(called) == 1
    assert called[0] == (ConfidenceLevel.STATIC_GUARD, ConfidenceLevel.LEARNING)


# ---------------------------------------------------------------------------
# Phase 4: Change detection — diff_discovery
# ---------------------------------------------------------------------------

def test_diff_discovery_service_added():
    old = {"services": [{"name": "nginx"}], "network_listeners": []}
    new = {"services": [{"name": "nginx"}, {"name": "mysql"}], "network_listeners": []}
    changes = diff_discovery(old, new)
    types = [c["change_type"] for c in changes]
    assert "SERVICE_ADDED" in types
    names = [c["entity_name"] for c in changes]
    assert "mysql" in names


def test_diff_discovery_service_removed():
    old = {"services": [{"name": "nginx"}, {"name": "redis"}], "network_listeners": []}
    new = {"services": [{"name": "nginx"}], "network_listeners": []}
    changes = diff_discovery(old, new)
    types = [c["change_type"] for c in changes]
    assert "SERVICE_REMOVED" in types
    names = [c["entity_name"] for c in changes]
    assert "redis" in names


def test_diff_discovery_port_opened():
    old = {"services": [], "network_listeners": [{"proto": "tcp", "port": "80"}]}
    new = {"services": [], "network_listeners": [{"proto": "tcp", "port": "80"}, {"proto": "tcp", "port": "443"}]}
    changes = diff_discovery(old, new)
    types = [c["change_type"] for c in changes]
    assert "PORT_OPENED" in types


def test_diff_discovery_no_changes():
    snap = {"services": [{"name": "nginx"}], "network_listeners": [{"proto": "tcp", "port": "80"}]}
    changes = diff_discovery(snap, snap)
    assert changes == []


@pytest.mark.asyncio
async def test_discovery_snapshot_save_and_load(redis):
    from remote_agent.discovery import save_discovery_snapshot, load_discovery_snapshot
    snap = {"services": [{"name": "nginx"}], "network_listeners": []}
    await save_discovery_snapshot(redis, tenant_id="t1", agent_id="a1", snapshot=snap)
    loaded = await load_discovery_snapshot(redis, tenant_id="t1", agent_id="a1")
    assert loaded == snap


@pytest.mark.asyncio
async def test_load_snapshot_returns_none_when_missing(redis):
    from remote_agent.discovery import load_discovery_snapshot
    result = await load_discovery_snapshot(redis, tenant_id="no-tenant", agent_id="no-agent")
    assert result is None


# ---------------------------------------------------------------------------
# Phase 5: Telegram doc-upload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_doc_upload_ignored_without_reply(redis):
    ctx = _ctx(redis)
    update = {
        "update_id": 1,
        "message": {
            "message_id": 100,
            "chat": {"id": 999},
            "document": {"file_id": "abc", "file_name": "test.pdf"},
            # No reply_to_message
        },
    }
    result = await handle_telegram_doc_upload(ctx, update)
    assert result is False


@pytest.mark.asyncio
async def test_doc_upload_ignored_without_pending_q(redis):
    ctx = _ctx(redis)
    update = {
        "update_id": 1,
        "message": {
            "message_id": 100,
            "chat": {"id": 999},
            "document": {"file_id": "abc", "file_name": "test.pdf"},
            "reply_to_message": {"message_id": 77},
        },
    }
    result = await handle_telegram_doc_upload(ctx, update)
    # No pending_q key in Redis → skip
    assert result is False


@pytest.mark.asyncio
async def test_doc_upload_returns_false_for_text_message(redis):
    ctx = _ctx(redis)
    update = {
        "update_id": 1,
        "message": {
            "message_id": 100,
            "chat": {"id": 999},
            "text": "hello",
        },
    }
    result = await handle_telegram_doc_upload(ctx, update)
    assert result is False


@pytest.mark.asyncio
async def test_doc_upload_processes_when_pending_q_exists(redis):
    from services.knowledge.document_store import get_doc

    # Pre-populate pending_q
    chat_id = 888
    bot_msg_id = 42
    q_key = "omni:knowledge:pending_q:t3:abc123"
    q_data = {
        "tenant_id": "t3",
        "agent_id": "agent-xyz",
        "hostname": "myhost",
        "entity_type": "process",
        "entity_name": "unknown_process",
    }
    await redis.set(q_key, json.dumps(q_data), ex=3600)
    await redis.set(f"omni:knowledge:pending_q_by_msgid:{chat_id}:{bot_msg_id}", q_key, ex=3600)

    ctx = _ctx(redis)
    update = {
        "update_id": 1,
        "message": {
            "message_id": 200,
            "chat": {"id": chat_id},
            "document": {
                "file_id": "TG-FILE-ID-001",
                "file_name": "runbook.pdf",
                "mime_type": "application/pdf",
            },
            "caption": "This is the nginx runbook",
            "reply_to_message": {"message_id": bot_msg_id},
        },
    }
    result = await handle_telegram_doc_upload(ctx, update)
    assert result is True

    # pending_q should be cleaned up
    remaining = await redis.get(q_key)
    assert remaining is None

    # Confidence should have increased
    score = await get_confidence_score(redis, tenant_id="t3", host="myhost")
    assert score >= 20
