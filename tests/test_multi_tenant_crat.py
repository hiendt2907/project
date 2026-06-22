"""Tests for multi-tenant CRAT support in chain_writer.py."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.audit_ledger.chain_writer as _cw
from services.audit_ledger.chain_writer import write_audit_block, _tenant_keys


@pytest.fixture(autouse=True)
def reset_lock():
    """Reset the global asyncio.Lock between tests so they each get a fresh one."""
    _cw._LOCK = None
    yield
    _cw._LOCK = None


# ─── Helpers ─────────────────────────────────────────────────────────────────


class _FakeKafka:
    """Captures Kafka publishes."""

    def __init__(self):
        self.messages: list[tuple[str, dict, bytes | None]] = []

    async def send_dict(self, topic: str, payload: dict, *, key: bytes | None = None) -> None:
        self.messages.append((topic, payload, key))


def _make_redis(initial: dict | None = None) -> MagicMock:
    """Build a minimal async Redis mock with pipeline support."""
    store: dict[str, object] = dict(initial or {})
    counters: dict[str, int] = {}
    lists: dict[str, list] = {}

    redis = MagicMock()

    class _Pipe:
        def __init__(self):
            self._ops: list = []

        def get(self, key):
            self._ops.append(("get", key))
            return self

        def incr(self, key):
            self._ops.append(("incr", key))
            return self

        def set(self, key, value):
            self._ops.append(("set", key, value))
            return self

        def rpush(self, key, value):
            self._ops.append(("rpush", key, value))
            return self

        async def execute(self):
            results = []
            for op in self._ops:
                if op[0] == "get":
                    results.append(store.get(op[1]))
                elif op[0] == "incr":
                    counters[op[1]] = counters.get(op[1], 0) + 1
                    store[op[1]] = counters[op[1]]
                    results.append(counters[op[1]])
                elif op[0] == "set":
                    store[op[1]] = op[2]
                    results.append("OK")
                elif op[0] == "rpush":
                    if op[1] not in lists:
                        lists[op[1]] = []
                    lists[op[1]].append(op[2])
                    results.append(len(lists[op[1]]))
            return results

    redis.pipeline.side_effect = lambda: _Pipe()
    # Expose internal state for assertions
    redis._store = store
    redis._lists = lists
    return redis


async def _write(redis, kafka, *, tenant_id="default"):
    return await write_audit_block(
        event_type="ADVISORY_DECISION",
        trace_id="trace-001",
        payload={"action": "test"},
        redis=redis,
        kafka=kafka,
        kafka_topic="omni-audit-chain",
        tenant_id=tenant_id,
    )


# ─── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_tenant_uses_original_keys():
    """Default tenant must write to the original audit_chain:blocks key."""
    redis = _make_redis()
    kafka = _FakeKafka()

    await _write(redis, kafka, tenant_id="default")

    assert "audit_chain:blocks" in redis._lists, "default tenant must use audit_chain:blocks"
    assert len(redis._lists["audit_chain:blocks"]) == 1


@pytest.mark.asyncio
async def test_named_tenant_uses_prefixed_keys():
    """Named tenant 'acme' must write to audit_chain:acme:blocks."""
    redis = _make_redis()
    kafka = _FakeKafka()

    await _write(redis, kafka, tenant_id="acme")

    assert "audit_chain:acme:blocks" in redis._lists, "acme tenant must use prefixed blocks key"
    # Must NOT pollute default key
    assert "audit_chain:blocks" not in redis._lists


@pytest.mark.asyncio
async def test_tenant_chains_are_isolated():
    """Writing to 'acme' and 'globex' tenants must not cross-contaminate."""
    redis = _make_redis()
    kafka = _FakeKafka()

    await _write(redis, kafka, tenant_id="acme")
    await _write(redis, kafka, tenant_id="globex")

    assert "audit_chain:acme:blocks" in redis._lists
    assert "audit_chain:globex:blocks" in redis._lists
    assert len(redis._lists["audit_chain:acme:blocks"]) == 1
    assert len(redis._lists["audit_chain:globex:blocks"]) == 1
    # Blocks key must not bleed between tenants
    assert "audit_chain:blocks" not in redis._lists


@pytest.mark.asyncio
async def test_tenant_id_in_kafka_payload():
    """Kafka message must include tenant_id field."""
    redis = _make_redis()
    kafka = _FakeKafka()

    await _write(redis, kafka, tenant_id="acme")

    assert len(kafka.messages) == 1
    _topic, payload, _key = kafka.messages[0]
    assert payload.get("tenant_id") == "acme", f"Expected tenant_id='acme' in payload, got: {payload}"


@pytest.mark.asyncio
async def test_backward_compatible_no_tenant():
    """Callers that don't pass tenant_id must still work with original Redis keys."""
    redis = _make_redis()
    kafka = _FakeKafka()

    # Call without tenant_id — backward compat
    block = await write_audit_block(
        event_type="ADVISORY_DECISION",
        trace_id="trace-compat",
        payload={"legacy": True},
        redis=redis,
        kafka=kafka,
        kafka_topic="omni-audit-chain",
    )

    assert block["seq"] == 1
    assert block["tenant_id"] == "default"
    assert "audit_chain:blocks" in redis._lists


def test_tenant_keys_default():
    """_tenant_keys('default') returns the original unnamespaced keys."""
    head, seq, blocks = _tenant_keys("default")
    assert head == "audit_chain:head_hash"
    assert seq == "audit_chain:seq"
    assert blocks == "audit_chain:blocks"


def test_tenant_keys_named():
    """_tenant_keys('acme') returns properly namespaced keys."""
    head, seq, blocks = _tenant_keys("acme")
    assert head == "audit_chain:acme:head_hash"
    assert seq == "audit_chain:acme:seq"
    assert blocks == "audit_chain:acme:blocks"
