"""Coverage tests for services.audit_ledger.chain_writer.

No mocks (per repo policy): uses fakeredis.aioredis for Redis and a hand-rolled
in-memory FakeKafka with the ``send_dict`` contract expected by chain_writer.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Any

import fakeredis.aioredis
import pytest

from services.audit_ledger import chain_writer as cw
from services.audit_ledger.chain_writer import (
    _compute_block_hash,
    _get_lock,
    _payload_hash,
    _tenant_keys,
    write_audit_block,
)
from services.audit_ledger.signer import AuditLedgerError


# ── Hand-rolled fake Kafka (no unittest.mock) ─────────────────────────────────

class _FakeKafka:
    def __init__(self, fail: bool = False, fail_with: Exception | None = None) -> None:
        self.sent: list[tuple[str, dict[str, Any], bytes | None]] = []
        self._fail = fail
        self._fail_with = fail_with

    async def send_dict(self, topic: str, message: dict[str, Any], key: bytes | None = None) -> None:
        if self._fail:
            raise (self._fail_with or RuntimeError("kafka unavailable"))
        self.sent.append((topic, message, key))


# ── Pure helpers ──────────────────────────────────────────────────────────────

def test_tenant_keys_default_preserves_legacy_names():
    head, seq, blocks = _tenant_keys("default")
    assert head == "audit_chain:head_hash"
    assert seq == "audit_chain:seq"
    assert blocks == "audit_chain:blocks"


def test_tenant_keys_named_tenant_isolated():
    head, seq, blocks = _tenant_keys("acme")
    assert head == "audit_chain:acme:head_hash"
    assert seq == "audit_chain:acme:seq"
    assert blocks == "audit_chain:acme:blocks"


def test_payload_hash_is_deterministic():
    a = _payload_hash({"b": 1, "a": "x"})
    b = _payload_hash({"a": "x", "b": 1})
    assert a == b
    assert len(a) == 64


def test_compute_block_hash_matches_canonical_sha256():
    h = _compute_block_hash(7, "EVT", "trace-1", "2026-01-01T00:00:00", "p", "prev")
    expected = hashlib.sha256(b"7|EVT|trace-1|2026-01-01T00:00:00|p|prev").hexdigest()
    assert h == expected


@pytest.mark.asyncio
async def test_get_lock_singleton():
    """Two calls in the same event loop return the same lock instance."""
    a = _get_lock()
    b = _get_lock()
    assert a is b
    assert isinstance(a, asyncio.Lock)


# ── Full write_audit_block flow with fakeredis + FakeKafka ────────────────────

@pytest.fixture(autouse=True)
def _disable_audit_signing(monkeypatch):
    monkeypatch.delenv("OMNI_AUDIT_PRIVATE_KEY_PATH", raising=False)
    from services.audit_ledger import signer as _s
    _s._load_private_key.cache_clear()
    yield
    _s._load_private_key.cache_clear()


@pytest.fixture
def fake_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_write_audit_block_writes_genesis_and_advances_seq(fake_redis):
    kafka = _FakeKafka()
    block = await write_audit_block(
        event_type="ADVISORY_DECISION",
        trace_id="trace-aaa",
        payload={"verdict": "FAILURE", "score": 0.9},
        redis=fake_redis,
        kafka=kafka,
        kafka_topic="omni-audit-chain",
    )
    assert block["seq"] == 1
    assert block["event_type"] == "ADVISORY_DECISION"
    assert block["trace_id"] == "trace-aaa"
    assert block["prev_hash"] == "0" * 64
    assert block["tenant_id"] == "default"
    assert block["signature_hex"] is None  # signing disabled

    head, seq, blocks = _tenant_keys("default")
    assert await fake_redis.get(head) == block["block_hash"]
    assert int(await fake_redis.get(seq)) == 1
    stored = await fake_redis.lrange(blocks, 0, -1)
    assert len(stored) == 1
    persisted = json.loads(stored[0])
    assert persisted["block_hash"] == block["block_hash"]

    # Kafka publish
    assert len(kafka.sent) == 1
    topic, msg, key = kafka.sent[0]
    assert topic == "omni-audit-chain"
    assert key == b"1"
    assert msg["block_hash"] == block["block_hash"]


@pytest.mark.asyncio
async def test_write_audit_block_chains_two_blocks(fake_redis):
    kafka = _FakeKafka()
    b1 = await write_audit_block(
        event_type="A", trace_id="trace-one", payload={"x": 1},
        redis=fake_redis, kafka=kafka, kafka_topic="omni-audit-chain",
    )
    b2 = await write_audit_block(
        event_type="B", trace_id="trace-two", payload={"x": 2},
        redis=fake_redis, kafka=kafka, kafka_topic="omni-audit-chain",
    )
    assert b2["prev_hash"] == b1["block_hash"]
    assert b2["seq"] == 2
    # Kafka keys also advance
    assert kafka.sent[1][2] == b"2"


@pytest.mark.asyncio
async def test_write_audit_block_named_tenant_uses_isolated_keys(fake_redis):
    kafka = _FakeKafka()
    block = await write_audit_block(
        event_type="HITL_DECISION",
        trace_id="trace-tenant",
        payload={"approved": True},
        redis=fake_redis,
        kafka=kafka,
        kafka_topic="omni-audit-chain",
        tenant_id="acme",
    )
    assert block["tenant_id"] == "acme"
    head, seq, blocks = _tenant_keys("acme")
    assert head.startswith("audit_chain:acme:")
    assert await fake_redis.get(head) == block["block_hash"]
    # Default tenant chain remains untouched
    assert await fake_redis.get("audit_chain:head_hash") is None


@pytest.mark.asyncio
async def test_write_audit_block_without_kafka_logs_and_returns(fake_redis):
    block = await write_audit_block(
        event_type="MUTATION_TRAPPED",
        trace_id="trace-no-kafka",
        payload={"why": "shadow_os"},
        redis=fake_redis,
        kafka=None,
        kafka_topic="omni-audit-chain",
    )
    assert block["seq"] == 1
    # Redis still updated
    head, _, _ = _tenant_keys("default")
    assert await fake_redis.get(head) == block["block_hash"]


@pytest.mark.asyncio
async def test_write_audit_block_wraps_redis_failure_in_audit_ledger_error():
    class _BrokenRedis:
        def pipeline(self):  # noqa: D401
            raise RuntimeError("redis down")

    kafka = _FakeKafka()
    with pytest.raises(AuditLedgerError, match="audit_chain write failed"):
        await write_audit_block(
            event_type="ADVISORY_DECISION",
            trace_id="trace-t",
            payload={"a": 1},
            redis=_BrokenRedis(),
            kafka=kafka,
            kafka_topic="omni-audit-chain",
        )


@pytest.mark.asyncio
async def test_write_audit_block_propagates_explicit_audit_ledger_error(fake_redis):
    sentinel = AuditLedgerError("kafka boom")
    kafka = _FakeKafka(fail=True, fail_with=sentinel)
    with pytest.raises(AuditLedgerError, match="kafka boom"):
        await write_audit_block(
            event_type="ADVISORY_DECISION",
            trace_id="t-err",
            payload={"a": 1},
            redis=fake_redis,
            kafka=kafka,
            kafka_topic="omni-audit-chain",
        )


@pytest.mark.asyncio
async def test_write_audit_block_chain_verifiable_end_to_end(fake_redis):
    """Round-trip: writer → reader → verify_chain returns ok."""
    from services.audit_ledger.verifier import verify_chain

    kafka = _FakeKafka()
    for i in range(3):
        await write_audit_block(
            event_type="ADVISORY_DECISION",
            trace_id=f"trace-{i}",
            payload={"i": i},
            redis=fake_redis,
            kafka=kafka,
            kafka_topic="omni-audit-chain",
        )
    raw = await fake_redis.lrange("audit_chain:blocks", 0, -1)
    blocks = [json.loads(b) for b in raw]
    result = verify_chain(blocks)
    assert result.ok is True
    assert result.blocks_checked == 3
    assert result.first_broken_seq is None


def test_module_level_constants_exposed():
    assert cw.REDIS_AUDIT_HEAD_KEY == "audit_chain:head_hash"
    assert cw.REDIS_AUDIT_SEQ_KEY == "audit_chain:seq"
    assert cw.REDIS_AUDIT_BLOCKS_KEY == "audit_chain:blocks"
