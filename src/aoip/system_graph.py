"""System Graph Builder — Fact → topology/dependency edges (Slice 3).

Vì sao tồn tại: nâng AI từ "biết các service rời rạc" lên "hiểu hệ thống nối với
nhau thế nào". Đây là bước Discovery → Facts → System Graph → Understanding.

Edge KHÔNG phải noun mới (INV_NO_NEW_NOUNS): mỗi edge là một Fact quan hệ
(subject→predicate→obj) với predicate ∈ RELATIONAL_PREDICATES. Builder chỉ suy ra
edge từ HINT cấu trúc (nginx upstream, compose depends_on, env *_HOST, kết nối TCP
quan sát) — KHÔNG đọc nội dung file (INV_NO_DATA_EXFIL): hint là metadata topology
do collector tách sẵn, builder thuần suy luận.
"""
from __future__ import annotations

from aoip.objects import RELATIONAL_PREDICATES, Fact


def _node(name: str) -> str:
    """Chuẩn hóa định danh node: 'redis' → 'svc:redis'; giữ nguyên nếu đã có scheme."""
    name = name.strip()
    return name if ":" in name else f"svc:{name}"


def infer_edges(hints: list[dict], *, default_evidence: str = "agent.topology") -> list[Fact]:
    """Suy ra edge (Fact quan hệ) từ hint cấu trúc.

    Mỗi hint: {source, relation, target, evidence?, confidence?}. Quan hệ không
    thuộc RELATIONAL_PREDICATES bị loại (không bịa quan hệ ngoài vocabulary).
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
                subject=_node(source),
                predicate=relation,
                obj=_node(target),
                confidence=float(h.get("confidence", 0.8)),
                provenance=(h.get("evidence", default_evidence),),
            )
        )
    return edges
