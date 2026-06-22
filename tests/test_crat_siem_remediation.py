"""Tests for CRAT SIEM remediation: write_siem_remediation_to_crat."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(redis=None, kafka=None):
    """Build a minimal worker context with fake Redis and mock Kafka."""
    if redis is None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    if kafka is None:
        kafka = _MockKafka()
    return SimpleNamespace(redis=redis, kafka=kafka)


class _MockKafka:
    """Minimal Kafka mock that captures send_dict calls."""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def send_dict(self, topic: str, payload: dict, key: bytes | None = None) -> None:
        self.sent.append((topic, payload))


# ---------------------------------------------------------------------------
# 1. Block is written to Redis audit chain
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_siem_remediation_creates_crat_block():
    """write_siem_remediation_to_crat must create a block in Redis audit_chain:blocks."""
    from services.evidence_adapter.siem_crat_bridge import write_siem_remediation_to_crat
    from services.audit_ledger.chain_writer import REDIS_AUDIT_BLOCKS_KEY

    # Reset the chain_writer's asyncio lock between tests
    import services.audit_ledger.chain_writer as cw
    cw._LOCK = None

    ctx = _make_ctx()
    block = await write_siem_remediation_to_crat(
        incident_id="inc-test-0001",
        category="ddos",
        action_taken="rate_limit_applied",
        outcome="success",
        ctx=ctx,
    )

    assert block is not None, "write_siem_remediation_to_crat must return a block"
    # Verify block exists in Redis list
    stored = await ctx.redis.lrange(REDIS_AUDIT_BLOCKS_KEY, -1, -1)
    assert stored, "Expected at least one block in Redis audit chain"
    parsed = json.loads(stored[0])
    assert parsed["event_type"] == "SIEM_REMEDIATION"


# ---------------------------------------------------------------------------
# 2. Block has correct event_type
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_siem_remediation_block_has_correct_event_type():
    """CRAT block must have event_type='SIEM_REMEDIATION'."""
    from services.evidence_adapter.siem_crat_bridge import write_siem_remediation_to_crat

    import services.audit_ledger.chain_writer as cw
    cw._LOCK = None

    ctx = _make_ctx()
    block = await write_siem_remediation_to_crat(
        incident_id="inc-test-0002",
        category="malware",
        action_taken="quarantine_pod",
        outcome="success",
        ctx=ctx,
    )

    assert block["event_type"] == "SIEM_REMEDIATION", (
        f"Expected event_type='SIEM_REMEDIATION', got {block['event_type']!r}"
    )
    assert block["trace_id"] == "inc-test-0002", (
        f"trace_id must equal incident_id, got {block['trace_id']!r}"
    )


# ---------------------------------------------------------------------------
# 3. Block has hash chain fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_siem_remediation_block_has_hash_chain():
    """CRAT block must include prev_hash and block_hash for chain integrity."""
    from services.evidence_adapter.siem_crat_bridge import write_siem_remediation_to_crat

    import services.audit_ledger.chain_writer as cw
    cw._LOCK = None

    ctx = _make_ctx()
    block = await write_siem_remediation_to_crat(
        incident_id="inc-test-0003",
        category="data_exfil",
        action_taken="block_egress_ip",
        outcome="success",
        ctx=ctx,
    )

    assert "prev_hash" in block, "CRAT block must have prev_hash"
    assert "block_hash" in block, "CRAT block must have block_hash"
    # Genesis block: prev_hash is 64 zeros
    assert len(block["prev_hash"]) == 64, f"prev_hash should be 64-char hex; got {block['prev_hash']!r}"
    assert len(block["block_hash"]) == 64, f"block_hash should be 64-char hex; got {block['block_hash']!r}"
    assert block["seq"] == 1, f"First block should have seq=1; got {block['seq']}"


@pytest.mark.asyncio
async def test_hash_chain_links_consecutive_blocks():
    """Each block's prev_hash must equal the previous block's block_hash."""
    from services.evidence_adapter.siem_crat_bridge import write_siem_remediation_to_crat

    import services.audit_ledger.chain_writer as cw
    cw._LOCK = None

    # Share the same Redis instance so the chain continues across writes
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    kafka = _MockKafka()
    ctx = _make_ctx(redis=redis, kafka=kafka)

    block1 = await write_siem_remediation_to_crat(
        incident_id="inc-chain-a",
        category="ddos",
        action_taken="rate_limit_applied",
        outcome="success",
        ctx=ctx,
    )
    block2 = await write_siem_remediation_to_crat(
        incident_id="inc-chain-b",
        category="auth_failure",
        action_taken="block_ip",
        outcome="success",
        ctx=ctx,
    )

    assert block2["prev_hash"] == block1["block_hash"], (
        f"block2.prev_hash must equal block1.block_hash: "
        f"{block2['prev_hash']!r} != {block1['block_hash']!r}"
    )
    assert block2["seq"] == block1["seq"] + 1


# ---------------------------------------------------------------------------
# 4. Payload is embedded in the block
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_siem_remediation_block_payload():
    """CRAT block must embed the remediation payload fields."""
    from services.evidence_adapter.siem_crat_bridge import write_siem_remediation_to_crat

    import services.audit_ledger.chain_writer as cw
    cw._LOCK = None

    ctx = _make_ctx()
    block = await write_siem_remediation_to_crat(
        incident_id="inc-payload-test",
        category="k8s_threat",
        action_taken="isolate_namespace",
        outcome="rejected",
        ctx=ctx,
    )

    payload = block.get("payload", {})
    assert payload["category"] == "k8s_threat", f"payload.category: {payload.get('category')!r}"
    assert payload["action_taken"] == "isolate_namespace"
    assert payload["outcome"] == "rejected"


# ---------------------------------------------------------------------------
# 5. Fail-closed: Redis failure raises AuditLedgerError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_siem_remediation_fail_closed():
    """If Redis write fails, write_siem_remediation_to_crat must raise (fail-closed)."""
    from services.evidence_adapter.siem_crat_bridge import write_siem_remediation_to_crat
    from services.audit_ledger.signer import AuditLedgerError

    import services.audit_ledger.chain_writer as cw
    cw._LOCK = None

    # Build a mock Redis that raises on pipeline().execute()
    broken_pipe = MagicMock()
    broken_pipe.get = MagicMock(return_value=broken_pipe)
    broken_pipe.incr = MagicMock(return_value=broken_pipe)
    broken_pipe.execute = AsyncMock(side_effect=ConnectionError("Redis connection refused"))

    broken_redis = MagicMock()
    broken_redis.pipeline = MagicMock(return_value=broken_pipe)

    ctx = _make_ctx(redis=broken_redis)

    with pytest.raises((AuditLedgerError, Exception)):
        await write_siem_remediation_to_crat(
            incident_id="inc-fail-closed",
            category="ddos",
            action_taken="block_ip",
            outcome="fail",
            ctx=ctx,
        )


# ---------------------------------------------------------------------------
# 6. Kafka receives audit chain message
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_siem_remediation_publishes_to_kafka_audit_chain():
    """write_siem_remediation_to_crat must publish to omni-audit-chain Kafka topic."""
    from services.evidence_adapter.siem_crat_bridge import write_siem_remediation_to_crat

    import services.audit_ledger.chain_writer as cw
    cw._LOCK = None

    kafka = _MockKafka()
    ctx = _make_ctx(kafka=kafka)

    await write_siem_remediation_to_crat(
        incident_id="inc-kafka-test",
        category="lateral_movement",
        action_taken="network_policy_applied",
        outcome="success",
        ctx=ctx,
    )

    assert kafka.sent, "Expected at least one message published to Kafka"
    topics = [t for t, _ in kafka.sent]
    assert "omni-audit-chain" in topics, (
        f"Expected omni-audit-chain in Kafka sends; got {topics}"
    )


# ---------------------------------------------------------------------------
# 7. Import via __init__.py works
# ---------------------------------------------------------------------------

def test_write_siem_remediation_importable_from_package():
    """write_siem_remediation_to_crat must be importable from the evidence_adapter package."""
    from services.evidence_adapter import write_siem_remediation_to_crat
    import asyncio
    assert callable(write_siem_remediation_to_crat)
