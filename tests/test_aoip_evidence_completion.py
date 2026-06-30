"""Tests Slice 4: Evidence Completion Engine (INV_INFER_BEFORE_ASK).

Senior KHÔNG hỏi ngay. Mỗi Unknown đi qua THANG bằng chứng: infer → runtime →
document → peer host → (cuối cùng) interview. Chỉ Unknown không nguồn nào chứng
minh mới thành câu hỏi. KPI = tối thiểu hóa câu hỏi. Resolver sinh Fact (provenance
= phương pháp); KHÔNG noun mới (gap = Hypothesis; câu hỏi = Communication).
"""
from __future__ import annotations

from aoip.capability import CapabilityState
from aoip.evidence import (
    DocumentResolver,
    EvidenceCompletionEngine,
    InferenceResolver,
    PeerHostResolver,
    RuntimeResolver,
    complete_evidence,
)
from aoip.objects import Communication
from aoip.system_graph import infer_edges
from aoip.system_model import SystemModel
from aoip.understanding import UnderstandingContext


def _ctx(model: SystemModel) -> UnderstandingContext:
    from aoip.discovery_backend import VMProfileDiscoveryBackend

    return UnderstandingContext(
        host="web-01",
        scope="acme/web-01",
        backend=VMProfileDiscoveryBackend({}),
        capability=CapabilityState(capability_id="complete_evidence", scope="acme/web-01"),
        model=model,
    )


def _model_with_gaps(*targets: str) -> SystemModel:
    hints = [
        {"source_type": "service", "source": "payment-api", "relation": "reads",
         "target_type": "database", "target": t.split(":", 1)[1], "evidence": "env.DB_HOST"}
        for t in targets
    ]
    return SystemModel(scope="s").fold(*infer_edges(hints))


async def test_runtime_resolution_means_no_question():
    # db:orders giải được bằng runtime probe → KHÔNG hỏi người.
    model = _model_with_gaps("db:orders")
    ctx = _ctx(model)
    engine = EvidenceCompletionEngine([
        InferenceResolver(),
        RuntimeResolver(prober=lambda node: "host:db-02" if node == "db:orders" else None),
    ])
    report = await complete_evidence(ctx, engine)

    assert report.asked == ()
    assert report.resolved["db:orders"] == "runtime"
    assert ctx.communications == []
    # node đã được chứng minh → rời khỏi tập Unknown.
    assert "db:orders" not in ctx.model.unknown_edge_targets


async def test_escalation_order_infer_before_runtime_before_doc():
    # cả ba resolver đều giải được db:orders; phải dừng ở INFER (rẻ nhất).
    model = _model_with_gaps("db:orders")
    ctx = _ctx(model)
    order_log: list[str] = []

    class Spy(InferenceResolver):
        async def resolve(self, node, m):
            order_log.append("infer")
            from aoip.objects import Fact
            return Fact(node, "observed_via", "inference", 0.7, ("graph.inference",))

    engine = EvidenceCompletionEngine([
        Spy(),
        RuntimeResolver(prober=lambda n: (order_log.append("runtime"), "x")[1]),
    ])
    report = await complete_evidence(ctx, engine)
    assert order_log == ["infer"]  # runtime không bao giờ được gọi
    assert report.resolved["db:orders"] == "inference"


async def test_unresolvable_gap_becomes_smart_question_with_evidence():
    model = _model_with_gaps("db:orders")
    ctx = _ctx(model)
    engine = EvidenceCompletionEngine([
        InferenceResolver(),
        RuntimeResolver(prober=lambda n: None),
        DocumentResolver(index={}),
        PeerHostResolver(registry={}),
    ])
    report = await complete_evidence(ctx, engine)

    assert report.asked == ("db:orders",)
    assert len(ctx.communications) == 1
    q = ctx.communications[0]
    assert isinstance(q, Communication)
    assert q.blocking_unknown == "db:orders"
    # câu hỏi ở TẦNG kiến trúc: nhắc service tham chiếu + nguồn evidence.
    assert "payment-api" in q.question
    assert "env.DB_HOST" in q.question


async def test_kpi_minimizes_questions_across_many_gaps():
    targets = [f"db:shard{i}" for i in range(10)]
    model = _model_with_gaps(*targets)
    ctx = _ctx(model)
    # 4 giải bằng runtime, 3 bằng document, 3 không giải được.
    runtime_ok = {f"db:shard{i}" for i in range(4)}
    doc_ok = {f"db:shard{i}": "host:dbx" for i in range(4, 7)}
    engine = EvidenceCompletionEngine([
        InferenceResolver(),
        RuntimeResolver(prober=lambda n: "host:r" if n in runtime_ok else None),
        DocumentResolver(index=doc_ok),
        PeerHostResolver(registry={}),
    ])
    report = await complete_evidence(ctx, engine)

    assert report.total_gaps == 10
    assert report.resolved_count == 7
    assert report.asked_count == 3
    assert report.inference_rate == 0.7
    # KPI: số câu hỏi ≪ số Unknown.
    assert len(ctx.communications) == 3


async def test_inference_resolves_node_with_corroborating_outgoing_edges():
    # payment-api vừa là target (proxies_to) vừa là subject (reads) → có tri thức
    # bổ trợ → INFER chứng minh tồn tại, không cần hỏi.
    edges = infer_edges([
        {"source": "nginx", "relation": "proxies_to", "target": "payment-api"},
        {"source": "payment-api", "relation": "reads", "target_type": "database", "target": "orders"},
    ])
    model = SystemModel(scope="s").fold(*edges)
    ctx = _ctx(model)
    engine = EvidenceCompletionEngine([InferenceResolver()])
    report = await complete_evidence(ctx, engine)
    assert report.resolved.get("svc:payment-api") == "inference"
