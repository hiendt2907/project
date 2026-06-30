"""Tests vertical slice understand_host — runtime ép buộc framework.

Bài kiểm thử thật: capability chạy end-to-end (discover→verify→model→interview→
assess); Observation→Hypothesis→Fact đúng vòng; SystemModel bất biến gấp Fact;
Unknown sinh Communication (never assume); CapabilityState chiều K đóng vòng.
Không noun mới.
"""
from __future__ import annotations

import pytest

from aoip.capabilities.understand_host import understand_host
from aoip.capability import CapabilityState, Maturity
from aoip.discovery_backend import MockHostDiscoveryBackend
from aoip.objects import Communication, Fact
from aoip.system_model import SystemModel
from aoip.understanding import UnderstandingContext


def _ctx(backend: MockHostDiscoveryBackend) -> UnderstandingContext:
    return UnderstandingContext(
        host="web-01",
        scope="payment/web-01",
        backend=backend,
        capability=CapabilityState(capability_id="understand_host", scope="payment/web-01"),
        model=SystemModel(scope="payment/web-01"),
    )


async def test_discovers_verifies_and_builds_system_model():
    ctx = _ctx(MockHostDiscoveryBackend())  # redis+nginx mở, postgres không
    await understand_host(ctx)

    # postgres không reachable → KHÔNG thành Fact runs_service.
    services = {f.obj for f in ctx.model.facts if f.predicate == "runs_service"}
    assert services == {"redis", "nginx"}
    assert "postgres" not in services
    # SystemModel có entity host.
    assert ctx.model.entities == {"host:web-01"}
    # Mọi Fact mang provenance (Provenance chain).
    assert all(f.provenance for f in ctx.model.facts)


async def test_unknown_triggers_interview_not_assumption():
    ctx = _ctx(MockHostDiscoveryBackend())
    await understand_host(ctx)

    assert len(ctx.communications) == 1
    comm = ctx.communications[0]
    assert isinstance(comm, Communication)
    assert comm.blocking_unknown == "service_owner:redis"
    # Never assume: không có Fact nào tự bịa owner của redis.
    assert not any(f.predicate == "owned_by" for f in ctx.model.facts)


async def test_assess_closes_loop_on_knowledge_dimension():
    ctx = _ctx(MockHostDiscoveryBackend())
    await understand_host(ctx)

    # coverage = verified(2) / (verified 2 + unknown 1) = 0.667 → K.
    assert ctx.capability.dimensions["K"] == pytest.approx(2 / 3, abs=1e-3)
    # K < 1 kéo score (=Π) xuống dưới 1 (INV_CAPABILITY_IS_PRODUCT).
    assert ctx.capability.score < 1.0
    assert ctx.capability.maturity is Maturity.DEVELOPING


async def test_full_coverage_when_all_verified_and_no_unknowns():
    backend = MockHostDiscoveryBackend(
        inventory={"services": [{"name": "redis", "port": 6379}], "unknowns": []},
        open_ports={6379},
    )
    ctx = _ctx(backend)
    await understand_host(ctx)

    assert ctx.communications == []
    # Coverage đầy đủ → K=1.0. (score tổng vẫn 0 vì E=0: capability quan-sát chưa
    # từng thực thi mutation — đúng INV_CAPABILITY_IS_PRODUCT.)
    assert ctx.capability.dimensions["K"] == pytest.approx(1.0)
    assert ctx.capability.dimensions["E"] == 0.0


async def test_system_model_is_immutable_and_supersedes_on_refold():
    base = SystemModel(scope="s")
    f1 = Fact("host:a", "exposes_port", "6379", 0.5, ("o1",), verified_time=1.0)
    m1 = base.fold(f1)
    # fold trả model MỚI; bản gốc không đổi (immutable).
    assert base.facts == ()
    assert len(m1.facts) == 1
    # Fact verify mới hơn cho cùng triple → supersede, không nhân đôi.
    f2 = Fact("host:a", "exposes_port", "6379", 0.99, ("o2",), verified_time=2.0)
    m2 = m1.fold(f2)
    assert len(m2.facts) == 1
    assert m2.facts[0].confidence == 0.99
