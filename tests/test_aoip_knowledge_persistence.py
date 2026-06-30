"""Tests: Human answer → persistent Fact → reuse → sống sót restart/reinstall.

KPI sống còn (không phải framework): câu hỏi của agent được người trả lời trở
thành Fact BỀN; mission sau nạp lại Knowledge nên KHÔNG hỏi lại; tri thức tồn tại
qua restart tiến trình (instance store mới đọc cùng file). Dùng noun đã có
(Fact/Communication/SystemModel) — KHÔNG model mới.
"""
from __future__ import annotations

from aoip.knowledge.store import FileKnowledgeStore, answer_question, seed_model
from aoip.objects import Communication, Fact
from aoip.system_model import SystemModel


def test_facts_persist_across_store_restart(tmp_path):
    path = tmp_path / "kb.json"
    store = FileKnowledgeStore(path)
    f = Fact("svc:cust-db", "resolved_as", "Aurora PostgreSQL", 0.95, ("human:interview",))
    store.save_facts("acme", "acme/web-01", [f])

    # Tiến trình MỚI (reinstall): instance store khác, cùng file → vẫn còn.
    reborn = FileKnowledgeStore(path)
    loaded = reborn.load_facts("acme", "acme/web-01")
    assert len(loaded) == 1
    assert loaded[0].subject == "svc:cust-db"
    assert loaded[0].obj == "Aurora PostgreSQL"
    assert loaded[0].provenance == ("human:interview",)


def test_answer_question_creates_human_fact():
    comm = Communication(
        question="cust-db ở đâu?", scope="acme/web-01", blocking_unknown="svc:cust-db"
    )
    fact = answer_question(comm, "AWS RDS Aurora")
    assert isinstance(fact, Fact)
    assert fact.subject == "svc:cust-db"
    assert "Aurora" in fact.obj
    assert fact.provenance == ("human:interview",)


def test_seeded_human_fact_marks_node_known_no_more_question():
    # Edge payment-api → svc:cust-db; chưa biết → Unknown.
    from aoip.system_graph import infer_edges
    edges = infer_edges([
        {"source": "payment-api", "relation": "depends_on", "target": "cust-db"},
    ])
    model = SystemModel(scope="s").fold(*edges)
    assert "svc:cust-db" in model.unknown_edge_targets

    # Người trả lời → Fact bền → seed vào model → node thành known → hết Unknown.
    human = answer_question(
        Communication(question="?", scope="s", blocking_unknown="svc:cust-db"),
        "Aurora",
    )
    model2 = seed_model(model, [human])
    assert "svc:cust-db" not in model2.unknown_edge_targets


def test_round_trip_two_runs_question_then_zero(tmp_path):
    """Run1 hỏi; trả lời + persist; Run2 (store mới) nạp lại → 0 câu hỏi."""
    from aoip.system_graph import infer_edges

    path = tmp_path / "kb.json"
    edges = infer_edges([{"source": "payment-api", "relation": "depends_on", "target": "cust-db"}])

    # Run 1: chưa có knowledge → còn Unknown → sinh + lưu câu trả lời người.
    store1 = FileKnowledgeStore(path)
    model1 = seed_model(SystemModel(scope="s").fold(*edges), store1.load_facts("acme", "s"))
    assert "svc:cust-db" in model1.unknown_edge_targets
    human = answer_question(Communication(question="?", scope="s", blocking_unknown="svc:cust-db"), "Aurora")
    store1.save_facts("acme", "s", [human])

    # Run 2: tiến trình mới, nạp knowledge bền → 0 Unknown.
    store2 = FileKnowledgeStore(path)
    model2 = seed_model(SystemModel(scope="s").fold(*edges), store2.load_facts("acme", "s"))
    assert model2.unknown_edge_targets == frozenset()
