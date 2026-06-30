"""Tests: System Graph là KNOWLEDGE GRAPH tổng quát, không phải dependency-graph.

Node có nhiều LOẠI (service/host/database/api/team/runbook…) mã hóa bằng tiền tố
định danh; edge dùng vocabulary chuẩn hóa (runs_on/owns/calls/reads/writes/
protected_by/observed_from…). API/Network/Ownership graph = projection của cùng
một KG. KHÔNG noun mới: node = định danh trong Fact; edge = Fact quan hệ.
"""
from __future__ import annotations

from aoip.objects import RELATIONAL_PREDICATES
from aoip.system_graph import infer_edges, make_node
from aoip.system_model import SystemModel


def test_typed_node_identifiers():
    assert make_node("service", "nginx") == "svc:nginx"
    assert make_node("host", "web-01") == "host:web-01"
    assert make_node("database", "orders") == "db:orders"
    assert make_node("api", "GET /pay") == "api:GET /pay"
    assert make_node("team", "payments") == "team:payments"
    # đã có scheme → giữ nguyên (idempotent).
    assert make_node("service", "host:web-01") == "host:web-01"


def test_normalized_edge_vocabulary_present():
    for rel in ("runs_on", "hosts", "owns", "calls", "depends_on", "proxies_to",
                "reads", "writes", "emits", "consumes", "protected_by", "observed_from"):
        assert rel in RELATIONAL_PREDICATES


def test_infer_edges_respects_node_types():
    hints = [
        {"source_type": "service", "source": "payment-api",
         "relation": "runs_on", "target_type": "host", "target": "web-01"},
        {"source_type": "service", "source": "payment-api",
         "relation": "reads", "target_type": "database", "target": "orders"},
        {"source_type": "team", "source": "payments",
         "relation": "owns", "target_type": "service", "target": "payment-api"},
    ]
    triples = {e.triple for e in infer_edges(hints)}
    assert ("svc:payment-api", "runs_on", "host:web-01") in triples
    assert ("svc:payment-api", "reads", "db:orders") in triples
    assert ("team:payments", "owns", "svc:payment-api") in triples


def test_projections_are_views_over_one_graph():
    edges = infer_edges([
        {"source": "nginx", "relation": "proxies_to", "target": "payment-api"},
        {"source_type": "service", "source": "payment-api",
         "relation": "reads", "target_type": "database", "target": "orders"},
        {"source_type": "team", "source": "payments",
         "relation": "owns", "target_type": "service", "target": "payment-api"},
        {"source_type": "service", "source": "payment-api",
         "relation": "runs_on", "target_type": "host", "target": "web-01"},
    ])
    m = SystemModel(scope="s").fold(*edges)
    # Ownership projection: chỉ edge 'owns'.
    own = m.project("owns")
    assert {e.triple for e in own} == {("team:payments", "owns", "svc:payment-api")}
    # Data-access projection: reads/writes.
    data = m.project("reads", "writes")
    assert {e.obj for e in data} == {"db:orders"}
    # Node theo loại.
    assert m.nodes_of_type("db") == frozenset({"db:orders"})
    assert m.nodes_of_type("host") == frozenset({"host:web-01"})
    assert "team:payments" in m.nodes_of_type("team")


def test_unknown_edge_target_generalizes_across_node_types():
    # payment-api reads db:orders nhưng db:orders chưa quan sát → Unknown Edge.
    m = SystemModel(scope="s").fold(*infer_edges([
        {"source_type": "service", "source": "payment-api",
         "relation": "reads", "target_type": "database", "target": "orders"},
    ]))
    assert "db:orders" in m.unknown_edge_targets
