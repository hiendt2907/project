"""System-topology Mermaid rendering (connects_to/proxies_to/depends_on edges).

The legacy per-probe diagrams (component/API-sequence/business-flow) only ever
drew per-host node facts — no cross-host edges. This covers the new
``render_system_topology_diagram`` (pure, facts -> Mermaid text) plus its wiring
into ``regenerate_diagrams`` via the persisted SystemModel.
"""
from __future__ import annotations

import time
from typing import Any

import fakeredis.aioredis
import pytest

from aoip.objects import Fact
from pkg.onboarding import discovery_doc as dd


def _redis() -> Any:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _fact(subject: str, predicate: str, obj: str) -> Fact:
    ts = time.time()
    return Fact(
        subject=subject, predicate=predicate, obj=obj,
        confidence=0.7, provenance=("test",),
        observation_time=ts, verified_time=ts,
    )


class TestRenderSystemTopologyDiagram:
    def test_connects_to_fact_renders_as_edge(self):
        edges = [_fact("host:web-01", "connects_to", "host:db-01")]
        text = dd.render_system_topology_diagram(edges)
        assert "graph" in text
        assert "connects_to" in text
        assert "-->" in text

    def test_node_shape_differs_by_entity_type(self):
        edges = [
            _fact("host:web-01", "connects_to", "host:db-01"),
            _fact("host:web-01", "depends_on", "db:orders"),
            _fact("host:web-01", "calls", "api:checkout"),
        ]
        text = dd.render_system_topology_diagram(edges)
        # host -> stadium, db -> cylinder, api -> hexagon: three distinct shapes present
        assert "([" in text  # host stadium open
        assert "[(" in text  # db cylinder open
        assert "{{" in text  # api hexagon open

    def test_empty_relational_facts_still_produces_valid_diagram(self):
        text = dd.render_system_topology_diagram([])
        assert text.strip().startswith("graph")
        assert "no relational facts" in text.lower()

    @pytest.mark.asyncio
    async def test_regenerate_diagrams_includes_connects_to_edge_from_system_model(self):
        from aoip.system_model_store import fold_and_persist

        r = _redis()
        await dd.accumulate_probe_fact(r, "acme", "service_topology", {"services": [{"name": "api"}]})
        await fold_and_persist(
            r, "acme", [_fact("host:web-01", "connects_to", "host:db-01")], source="test",
        )
        version = await dd.regenerate_diagrams(r, "acme")
        text = await dd.get_diagram_version(r, "acme", version)
        assert "connects_to" in text
        assert "host:web-01" in text or "web-01" in text

    @pytest.mark.asyncio
    async def test_regenerate_diagrams_no_edges_does_not_crash(self):
        r = _redis()
        await dd.accumulate_probe_fact(r, "acme", "process_list", {"processes": [{"name": "nginx"}]})
        version = await dd.regenerate_diagrams(r, "acme")
        text = await dd.get_diagram_version(r, "acme", version)
        assert "graph" in text
