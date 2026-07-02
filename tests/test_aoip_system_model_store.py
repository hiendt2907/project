"""Slice O1: persisted, versioned, per-tenant SystemModel store."""
from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest

from aoip.objects import Fact
from aoip.system_model_store import fold_and_persist, load_system_model


def _redis() -> Any:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _fact(subject: str, predicate: str, obj: str, *, provenance=("p1",), ts: float = 100.0) -> Fact:
    return Fact(
        subject=subject, predicate=predicate, obj=obj,
        confidence=0.9, provenance=provenance, observation_time=ts, verified_time=ts,
    )


class TestPersistReload:
    @pytest.mark.asyncio
    async def test_empty_tenant_has_revision_zero(self):
        r = _redis()
        model, revision = await load_system_model(r, "acme")
        assert revision == 0
        assert model.facts == ()

    @pytest.mark.asyncio
    async def test_fold_and_persist_increments_revision(self):
        r = _redis()
        _model, rev1, _ = await fold_and_persist(r, "acme", [_fact("host:web-01", "exposes_port", "80")], source="s1")
        assert rev1 == 1
        model, rev2 = await load_system_model(r, "acme")
        assert rev2 == 1
        assert model.facts_about("host:web-01")[0].obj == "80"

    @pytest.mark.asyncio
    async def test_revision_monotonically_increases_across_calls(self):
        r = _redis()
        await fold_and_persist(r, "acme", [_fact("host:a", "exposes_port", "1")], source="s1")
        _model, rev2, _ = await fold_and_persist(r, "acme", [_fact("host:a", "exposes_port", "2")], source="s1", )
        assert rev2 == 2

    @pytest.mark.asyncio
    async def test_no_op_fold_does_not_bump_revision(self):
        r = _redis()
        f = _fact("host:a", "exposes_port", "1")
        await fold_and_persist(r, "acme", [f], source="s1")
        _model, rev2, _ = await fold_and_persist(r, "acme", [f], source="s1")
        # same triple, same/older verified_time -> fold() is a no-op (index unchanged)
        assert rev2 == 1


class TestRestartRecovery:
    @pytest.mark.asyncio
    async def test_fresh_process_continues_revision_no_reset(self):
        """Simulates a worker restart: no in-memory state carried over — a brand
        new call sequence against the same Redis must resume at the persisted
        revision, not reset to 0. Process memory is never the source of truth."""
        r = _redis()
        await fold_and_persist(r, "acme", [_fact("host:a", "exposes_port", "1")], source="s1")
        await fold_and_persist(r, "acme", [_fact("host:a", "exposes_port", "2")], source="s1")

        # "Restart": forget everything except the redis handle, reload from scratch.
        reloaded_model, reloaded_revision = await load_system_model(r, "acme")
        assert reloaded_revision == 2
        assert reloaded_model.facts_about("host:a")[0].obj == "2"

        # Continuing to fold after "restart" keeps incrementing, does not reset.
        _model, rev3, _ = await fold_and_persist(
            r, "acme", [_fact("host:a", "exposes_port", "3")], source="s1",
        )
        assert rev3 == 3


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_tenant_a_cannot_read_tenant_b_twin(self):
        r = _redis()
        await fold_and_persist(r, "tenant-a", [_fact("host:x", "exposes_port", "1")], source="s1")
        model_b, revision_b = await load_system_model(r, "tenant-b")
        assert revision_b == 0
        assert model_b.facts == ()


class TestHistory:
    @pytest.mark.asyncio
    async def test_history_entry_recorded_per_fold(self):
        r = _redis()
        await fold_and_persist(r, "acme", [_fact("host:a", "exposes_port", "1")], source="discovery:port_scan")
        entries = await r.lrange("omni:aoip:system_model_history:acme", 0, -1)
        assert len(entries) == 1
        assert "discovery:port_scan" in entries[0]


class TestContradiction:
    @pytest.mark.asyncio
    async def test_same_source_temporal_replacement_supersedes(self):
        r = _redis()
        await fold_and_persist(
            r, "acme", [_fact("host:a", "runs_service", "nginx", provenance=("discovery:port_scan:tr1",), ts=100.0)],
            source="s1",
        )
        model, _rev, contradictions = await fold_and_persist(
            r, "acme",
            [_fact("host:a", "runs_service", "apache", provenance=("discovery:port_scan:tr1",), ts=500.0)],
            source="s1",
        )
        assert contradictions == []
        assert model.facts_about("host:a")[0].obj == "apache"

    @pytest.mark.asyncio
    async def test_conflicting_sources_same_window_flagged_not_overwritten(self):
        r = _redis()
        await fold_and_persist(
            r, "acme",
            [_fact("host:a", "runs_service", "nginx", provenance=("discovery:port_scan:tr1", "agent:agent-A"), ts=100.0)],
            source="s1",
        )
        model, _rev, contradictions = await fold_and_persist(
            r, "acme",
            [_fact("host:a", "runs_service", "apache", provenance=("discovery:port_scan:tr2", "agent:agent-B"), ts=105.0)],
            source="s1",
        )
        assert len(contradictions) == 1
        # old fact must still be present — no silent overwrite
        assert model.facts_about("host:a")[0].obj == "nginx"
        stored = await r.lrange("omni:aoip:contradictions:acme", 0, -1)
        assert len(stored) == 1
