"""TDD — services.siem_correlation.graph: Redis union-find graph correlator.

Parity contract: port 1-1 của brain-go ``internal/correlate/graph.go`` +
``chain.go::maybeEmitGraphChain`` — cùng key layout ``corr:*`` (prefix
configurable cho parity run), cùng gates (threshold / entity-span /
min-confidence / dedup SETNX).

Dùng FakeRedis(decode_responses=True) theo convention dự án (ZSET thật,
không AsyncMock).
"""

from __future__ import annotations

import pytest
from fakeredis.aioredis import FakeRedis

from services.siem_correlation.graph import GraphConfig, GraphCorrelator
from services.siem_correlation.models import Incident


def _cfg(**over) -> GraphConfig:
    base = dict(
        window_seconds=600,
        threshold=3,
        dedup_seconds=900,
        min_entity_span=1,
        min_confidence=0.5,
        w_entity=0.4,
        w_sequence=0.35,
        w_volume=0.25,
        key_prefix="corr:",
    )
    base.update(over)
    return GraphConfig(**base)


def _inc(id_: str, category: str, *, tenant: str = "t1", source_ip: str = "", raw_log: str = "", rule_id: str = "") -> Incident:
    from services.siem_correlation.entities import extract_entities

    inc = Incident(
        incident_id=id_, tenant_id=tenant, severity="medium", source="finguard",
        category=category, timestamp_unix=0, schema_version="1.0.0", rule_id=rule_id,
        source_ip=source_ip, dest_ip="", raw_log=raw_log,
        correlation_ids=(), tags=(), entities=(),
    )
    return inc.with_entities(extract_entities(inc))


@pytest.fixture
def redis():
    return FakeRedis(decode_responses=True)


class TestSharedIPChain:
    async def test_three_incidents_sharing_ip_emit_one_chain(self, redis):
        corr = GraphCorrelator(redis, _cfg())
        assert await corr.process(_inc("a", "port_scan", source_ip="10.0.0.5"), now=1000) is None
        assert await corr.process(_inc("b", "auth_failure", source_ip="10.0.0.5"), now=1010) is None
        chain = await corr.process(_inc("c", "new_process", source_ip="10.0.0.5"), now=1020)
        assert chain is not None
        assert chain["tenant_id"] == "t1"
        assert chain["attack_category"] == "execution"
        assert chain["kill_chain_ordered"] is True
        assert chain["common_dimensions"] == [{"type": "ip", "value": "10.0.0.5"}]
        ids = [m["incident_id"] for m in chain["member_events"]]
        assert ids == ["a", "b", "c"]
        # entity=1 type→0.333, seq=1.0 (1→2→3), volume=3/6=0.5
        assert chain["signals"] == {"entity": 0.333, "sequence": 1.0, "volume": 0.5, "confidence": 0.608}
        assert chain["confidence"] == 0.608
        assert chain["window_seconds"] == 600
        assert chain["schema_version"] == "1.0.0"

    async def test_dedup_suppresses_reemission_within_ttl(self, redis):
        corr = GraphCorrelator(redis, _cfg())
        for i, (id_, cat) in enumerate([("a", "port_scan"), ("b", "auth_failure"), ("c", "new_process")]):
            chain = await corr.process(_inc(id_, cat, source_ip="10.0.0.5"), now=1000 + i * 10)
        assert chain is not None
        again = await corr.process(_inc("d", "malware", source_ip="10.0.0.5"), now=1040)
        assert again is None

    async def test_below_threshold_no_chain(self, redis):
        corr = GraphCorrelator(redis, _cfg())
        assert await corr.process(_inc("a", "port_scan", source_ip="10.0.0.5"), now=1000) is None
        assert await corr.process(_inc("b", "auth_failure", source_ip="10.0.0.5"), now=1010) is None


class TestUnionAcrossEntities:
    async def test_transitive_linking_via_shared_user_and_host(self, redis):
        corr = GraphCorrelator(redis, _cfg())
        await corr.process(_inc("a", "auth_failure", source_ip="192.168.1.1", raw_log="user=alice"), now=2000)
        await corr.process(_inc("b", "new_process", raw_log="user=alice host=web-1"), now=2010)
        chain = await corr.process(_inc("c", "lateral_movement", source_ip="192.168.9.9", raw_log="host=web-1"), now=2020)
        assert chain is not None
        assert chain["attack_category"] == "lateral_movement"
        assert {m["incident_id"] for m in chain["member_events"]} == {"a", "b", "c"}
        # dims sorted (type, value): host, ip×2, user → 3 distinct types → entity=1.0
        assert chain["common_dimensions"] == [
            {"type": "host", "value": "web-1"},
            {"type": "ip", "value": "192.168.1.1"},
            {"type": "ip", "value": "192.168.9.9"},
            {"type": "user", "value": "alice"},
        ]
        assert chain["signals"]["entity"] == 1.0
        # orders 2→3→5 monotonic
        assert chain["kill_chain_ordered"] is True
        assert chain["confidence"] == 0.875

    async def test_no_entities_incident_is_ignored(self, redis):
        corr = GraphCorrelator(redis, _cfg())
        assert await corr.process(_inc("a", "auth_failure"), now=1000) is None
        # no state was created
        assert await redis.keys("corr:*") == []


class TestGates:
    async def test_min_entity_span_gate(self, redis):
        corr = GraphCorrelator(redis, _cfg(min_entity_span=2))
        for i, (id_, cat) in enumerate([("a", "port_scan"), ("b", "auth_failure"), ("c", "new_process")]):
            chain = await corr.process(_inc(id_, cat, source_ip="10.0.0.5"), now=1000 + i * 10)
        assert chain is None

    async def test_min_confidence_gate(self, redis):
        corr = GraphCorrelator(redis, _cfg(min_confidence=0.99))
        for i, (id_, cat) in enumerate([("a", "port_scan"), ("b", "auth_failure"), ("c", "new_process")]):
            chain = await corr.process(_inc(id_, cat, source_ip="10.0.0.5"), now=1000 + i * 10)
        assert chain is None

    async def test_window_prunes_stale_incidents(self, redis):
        corr = GraphCorrelator(redis, _cfg())
        await corr.process(_inc("a", "port_scan", source_ip="10.0.0.5"), now=100)
        # now=800 → window start 200 → "a" pruned from windows
        await corr.process(_inc("b", "auth_failure", source_ip="10.0.0.5"), now=800)
        chain = await corr.process(_inc("c", "new_process", source_ip="10.0.0.5"), now=810)
        assert chain is None


class TestTenantIsolation:
    async def test_same_entities_do_not_merge_across_tenants(self, redis):
        corr = GraphCorrelator(redis, _cfg())
        await corr.process(_inc("a1", "port_scan", tenant="tA", source_ip="10.0.0.5"), now=1000)
        await corr.process(_inc("b1", "auth_failure", tenant="tB", source_ip="10.0.0.5"), now=1005)
        await corr.process(_inc("a2", "auth_failure", tenant="tA", source_ip="10.0.0.5"), now=1010)
        await corr.process(_inc("b2", "new_process", tenant="tB", source_ip="10.0.0.5"), now=1015)
        chain_a = await corr.process(_inc("a3", "new_process", tenant="tA", source_ip="10.0.0.5"), now=1020)
        assert chain_a is not None
        assert chain_a["tenant_id"] == "tA"
        assert {m["incident_id"] for m in chain_a["member_events"]} == {"a1", "a2", "a3"}


class TestKeyPrefix:
    async def test_custom_prefix_isolates_state(self, redis):
        corr = GraphCorrelator(redis, _cfg(key_prefix="pycorr:"))
        await corr.process(_inc("a", "port_scan", source_ip="10.0.0.5"), now=1000)
        assert await redis.keys("corr:*") == []
        assert len(await redis.keys("pycorr:*")) > 0
