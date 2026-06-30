"""Knowledge Graph Builder — Fact → đồ thị tri thức tenant (Slice 3, tổng quát).

Vì sao tồn tại: nâng AI từ "biết các service rời rạc" lên "hiểu cả hệ thống của
khách hàng" — không chỉ topology service mà toàn bộ: hạ tầng, API, mạng, dữ liệu,
sở hữu, nghiệp vụ, bảo mật. Đây là nền cho Interview (hỏi ở tầng kiến trúc),
Causal Model và Incident Reasoning về sau.

Thiết kế Knowledge Graph (không phải dependency-graph riêng cho service):
  - NODE có nhiều LOẠI (service/host/container/port/api/database/queue/secret/
    firewall/team/runbook/business_capability…), mã hóa bằng TIỀN TỐ định danh.
  - EDGE dùng vocabulary chuẩn hóa (RELATIONAL_PREDICATES).
  - API/Network/Ownership/Business graph = PROJECTION (lọc predicate) trên cùng KG.

KHÔNG noun mới (INV_NO_NEW_NOUNS): node = định danh trong Fact; edge = Fact quan
hệ. INV_NO_DATA_EXFIL: builder nhận HINT metadata do collector tách sẵn, không
đọc nội dung file — builder thuần suy luận.
"""
from __future__ import annotations

from aoip.objects import RELATIONAL_PREDICATES, Fact

# Loại node → tiền tố định danh. Mở rộng loại = thêm 1 dòng, KHÔNG đổi kiến trúc.
NODE_TYPE_PREFIX: dict[str, str] = {
    "service": "svc",
    "host": "host",
    "container": "ctr",
    "port": "port",
    "api": "api",
    "database": "db",
    "queue": "queue",
    "secret": "secret",
    "firewall": "fw",
    "team": "team",
    "runbook": "runbook",
    "business_capability": "bcap",
    "document": "doc",
}


def make_node(node_type: str, name: str) -> str:
    """Định danh node có loại: ('database','orders') → 'db:orders'.

    Idempotent: tên đã mang scheme (vd 'host:web-01') được giữ nguyên. Loại lạ →
    dùng chính nó làm tiền tố (linh hoạt, không chặn mở rộng tương lai).
    """
    name = name.strip()
    if ":" in name:
        return name
    prefix = NODE_TYPE_PREFIX.get(node_type, node_type)
    return f"{prefix}:{name}"


def _node(name: str) -> str:
    """Tên trần (không rõ loại) → mặc định coi là service."""
    return make_node("service", name)


def infer_edges(hints: list[dict], *, default_evidence: str = "agent.topology") -> list[Fact]:
    """Suy ra edge (Fact quan hệ) từ hint cấu trúc — hỗ trợ node có loại.

    Mỗi hint: {source, relation, target, source_type?, target_type?, evidence?,
    confidence?}. ``*_type`` mặc định 'service'. Quan hệ ngoài vocabulary bị loại
    (không bịa predicate). Hint thiếu source/target bị bỏ qua.
    """
    edges: list[Fact] = []
    for h in hints:
        relation = h.get("relation", "")
        source = h.get("source", "")
        target = h.get("target", "")
        if relation not in RELATIONAL_PREDICATES or not source or not target:
            continue
        edges.append(
            Fact(
                subject=make_node(h.get("source_type", "service"), source),
                predicate=relation,
                obj=make_node(h.get("target_type", "service"), target),
                confidence=float(h.get("confidence", 0.8)),
                provenance=(h.get("evidence", default_evidence),),
            )
        )
    return edges
