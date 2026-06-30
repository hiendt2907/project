"""Tests Slice 3: System Graph Builder — Fact → topology/dependency graph.

AI bắt đầu "hiểu hệ thống": từ Fact rời rạc (nginx, redis, postgres) suy ra
QUAN HỆ (nginx proxies_to payment-api; payment depends_on redis/postgres). Edge
= Fact quan hệ (KHÔNG noun mới); graph = view derived trên SystemModel. Edge trỏ
tới service CHƯA quan sát được = Unknown Edge (hạt giống câu hỏi kiến trúc — slice
Interview sau).
"""
from __future__ import annotations

from aoip.capabilities.map_system_graph import map_system_graph
from aoip.capability import CapabilityState
from aoip.discovery_backend import VMProfileDiscoveryBackend
from aoip.objects import Fact
from aoip.system_graph import infer_edges
from aoip.system_model import SystemModel
from aoip.understanding import UnderstandingContext


def test_infer_edges_turns_hints_into_relational_facts():
    hints = [
        {"source": "nginx", "relation": "proxies_to", "target": "payment-api", "evidence": "nginx.upstream"},
        {"source": "payment-api", "relation": "depends_on", "target": "redis", "evidence": "compose.depends_on"},
        {"source": "weird", "relation": "talks_about", "target": "x"},  # quan hệ không hợp lệ → bỏ
    ]
    edges = infer_edges(hints)
    triples = {e.triple for e in edges}
    assert ("svc:nginx", "proxies_to", "svc:payment-api") in triples
    assert ("svc:payment-api", "depends_on", "svc:redis") in triples
    assert all(e.provenance for e in edges)
    assert len(edges) == 2  # quan hệ lạ bị loại


def test_system_model_graph_queries():
    base = SystemModel(scope="s")
    edges = infer_edges([
        {"source": "payment-api", "relation": "depends_on", "target": "redis"},
        {"source": "payment-api", "relation": "depends_on", "target": "postgres"},
        {"source": "nginx", "relation": "proxies_to", "target": "payment-api"},
    ])
    m = base.fold(*edges)
    assert set(m.dependencies_of("svc:payment-api")) == {"svc:redis", "svc:postgres"}
    assert set(m.dependents_of("svc:payment-api")) == {"svc:nginx"}
    assert len(m.edges) == 3


def test_unknown_edge_target_detected_when_service_never_observed():
    base = SystemModel(scope="s")
    observed = Fact("host:h", "runs_service", "redis", 0.9, ("o",))
    edges = infer_edges([
        {"source": "payment-api", "relation": "depends_on", "target": "redis"},      # đã quan sát
        {"source": "payment-api", "relation": "depends_on", "target": "analytics"},  # CHƯA quan sát
    ])
    m = base.fold(observed, *edges)
    assert "svc:analytics" in m.unknown_edge_targets
    assert "svc:redis" not in m.unknown_edge_targets


async def _run(profile: dict) -> UnderstandingContext:
    ctx = UnderstandingContext(
        host="web-01",
        scope="acme/web-01",
        backend=VMProfileDiscoveryBackend(profile),
        capability=CapabilityState(capability_id="map_system_graph", scope="acme/web-01"),
        model=SystemModel(scope="acme/web-01"),
    )
    await map_system_graph(ctx)
    return ctx


async def test_capability_builds_graph_from_discovery():
    profile = {
        "hostname": "web-01",
        "services": [{"name": "nginx", "status": "running"}, {"name": "redis", "status": "running"}],
        "listeners": [{"port": 80, "service": "nginx"}, {"port": 6379, "service": "redis"}],
        "relationships": [
            {"source": "nginx", "relation": "proxies_to", "target": "payment-api", "evidence": "nginx.upstream"},
            {"source": "payment-api", "relation": "depends_on", "target": "redis", "evidence": "env.REDIS_HOST"},
        ],
    }
    ctx = await _run(profile)
    # graph có edge.
    assert len(ctx.model.edges) == 2
    assert "svc:redis" in ctx.model.dependencies_of("svc:payment-api")
    # payment-api được nhắc nhưng chưa quan sát chạy → Unknown Edge (câu hỏi kiến trúc sau).
    assert "svc:payment-api" in ctx.model.unknown_edge_targets


async def test_no_relationships_yields_no_edges_no_crash():
    ctx = await _run({"hostname": "x", "services": [], "listeners": []})
    assert ctx.model.edges == ()
    assert ctx.model.unknown_edge_targets == frozenset()
