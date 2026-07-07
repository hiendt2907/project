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

    def test_services_grouped_into_edge_app_data_tiers(self):
        edges = [
            _fact("host:cust-edge", "hosts", "svc:nginx"),
            _fact("host:cust-app", "hosts", "svc:python3"),
            _fact("host:cust-db", "hosts", "svc:mariadbd"),
            _fact("host:cust-edge", "connects_to", "host:cust-app"),
            _fact("host:cust-app", "connects_to", "host:cust-db"),
        ]
        text = dd.render_system_topology_diagram(edges)
        assert 'subgraph tier_edge ["Edge / Gateway"]' in text
        assert 'subgraph tier_app ["Application"]' in text
        assert 'subgraph tier_data ["Data"]' in text
        # Edge tier appears before Application tier before Data tier (layout order).
        assert text.index("tier_edge") < text.index("tier_app") < text.index("tier_data")
        assert "nginx (cust-edge)" in text
        assert "mariadbd (cust-db)" in text

    def test_reciprocal_connects_to_facts_collapse_to_one_edge_ordered_edge_to_data(self):
        # connection_scan observes the same TCP link from both ends, so
        # facts often arrive in both directions for one logical connection.
        # Rendering both would form a 2-cycle and flip Mermaid's dagre
        # top-to-bottom tier ranking (Edge/App/Data).
        edges = [
            _fact("host:cust-edge", "hosts", "svc:nginx"),
            _fact("host:cust-app", "hosts", "svc:python3"),
            _fact("host:cust-edge", "connects_to", "host:cust-app"),
            _fact("host:cust-app", "connects_to", "host:cust-edge"),
        ]
        text = dd.render_system_topology_diagram(edges)
        # only one directed edge survives, not two reciprocal ones
        assert text.count("-->|connects_to|") == 1
        lines = text.splitlines()
        nginx_line = next(line for line in lines if "nginx (cust-edge)" in line)
        python_line = next(line for line in lines if "python3 (cust-app)" in line)
        nginx_id = nginx_line.strip().split("[")[0]
        python_id = python_line.strip().split("[")[0]
        arrow_line = next(line for line in lines if "-->|connects_to|" in line)
        # oriented edge(lower tier rank) -> app(higher tier rank), never the reverse
        assert arrow_line.strip().startswith(nginx_id)
        assert arrow_line.strip().endswith(python_id)

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


class TestRenderApiSequenceDiagram:
    def test_duplicate_port_scan_entries_render_one_edge(self):
        doc = {
            "port_scan": {
                "listening_ports": [
                    {"port": 6379, "service": "redis-server"},
                    {"port": 6379, "service": "redis-server"},
                ]
            }
        }
        text = dd.render_api_sequence_diagram(doc)
        assert text.count("client --> svc_6379") == 1
        assert text.count('svc_6379["redis-server (6379)"]') == 1

    def test_no_gateway_service_falls_back_to_flat_client_fanout(self):
        doc = {"port_scan": {"listening_ports": [{"port": 6379, "service": "redis-server"}]}}
        text = dd.render_api_sequence_diagram(doc)
        assert "client --> svc_6379" in text
        assert "Gateway" not in text

    def test_edge_tier_service_renders_as_gateway_between_client_and_backends(self):
        doc = {
            "port_scan": {
                "listening_ports": [
                    {"port": 80, "service": "nginx"},
                    {"port": 8080, "service": "python3"},
                ]
            }
        }
        text = dd.render_api_sequence_diagram(doc)
        assert "client --> svc_80" in text
        assert "Gateway" in text
        assert "svc_80 --> svc_8080" in text
        assert "client --> svc_8080" not in text
