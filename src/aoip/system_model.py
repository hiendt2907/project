"""SystemModel — Knowledge §5 / META_MODEL §5: mô hình hệ thống của một tenant.

Derived khỏi Fact đã verify (INV_DERIVED_NEVER_PERSIST từ góc nhìn truth: Fact là
nguồn, SystemModel là chỉ-mục/khung nhìn bất biến gấp từ Fact). Bất biến: mỗi lần
học thêm Fact → ``fold`` trả về model mới (immutable, coding-style).

Skeleton: lớp NHÂN-QUẢ (A gây B sau Δt) sẽ wire khi runtime cần (chưa suy diễn
trước — lộ trình Implementation→Architecture). Hiện model = tập Fact + chỉ-mục
entity/quan hệ, đủ cho "Day-1: Observe→Map".
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from aoip.objects import RELATIONAL_PREDICATES, Fact


@dataclass(frozen=True)
class SystemModel:
    scope: str
    facts: tuple[Fact, ...] = field(default_factory=tuple)

    def fold(self, *new_facts: Fact) -> "SystemModel":
        """Gấp Fact mới vào model → model MỚI (bất biến). Supersede triple trùng."""
        if not new_facts:
            return self
        index: dict[tuple[str, str, str], Fact] = {f.triple: f for f in self.facts}
        for f in new_facts:
            prev = index.get(f.triple)
            # Giữ Fact verify gần nhất (supersede theo verified_time).
            if prev is None or f.verified_time >= prev.verified_time:
                index[f.triple] = f
        return replace(self, facts=tuple(index.values()))

    @property
    def entities(self) -> frozenset[str]:
        """Derived: tập subject đã biết (substrate của Knowledge Graph)."""
        return frozenset(f.subject for f in self.facts)

    def facts_about(self, subject: str) -> tuple[Fact, ...]:
        return tuple(f for f in self.facts if f.subject == subject)

    # ── Graph view (derived; edge = Fact quan hệ) ────────────────────────────
    @property
    def edges(self) -> tuple[Fact, ...]:
        """Derived: tập Fact quan hệ (topology/dependency)."""
        return tuple(f for f in self.facts if f.predicate in RELATIONAL_PREDICATES)

    def dependencies_of(self, node: str) -> tuple[str, ...]:
        """Node mà ``node`` trỏ tới (out-edge): nó phụ thuộc/định tuyến vào ai."""
        return tuple(e.obj for e in self.edges if e.subject == node)

    def dependents_of(self, node: str) -> tuple[str, ...]:
        """Node trỏ tới ``node`` (in-edge): ai phụ thuộc vào nó."""
        return tuple(e.subject for e in self.edges if e.obj == node)

    def blast_radius(self, node: str) -> tuple[str, ...]:
        """Đóng bao bắc cầu của dependents: node hỏng → ai bị ảnh hưởng.

        Đây là reasoning sự cố thuần GRAPH (không LLM): BFS ngược theo edge phụ
        thuộc. Trả các node bị ảnh hưởng (không gồm chính node), ổn định thứ tự.
        """
        affected: list[str] = []
        seen = {node}
        queue = [node]
        while queue:
            current = queue.pop(0)
            for dep in self.dependents_of(current):
                if dep not in seen:
                    seen.add(dep)
                    affected.append(dep)
                    queue.append(dep)
        return tuple(affected)

    def project(self, *predicates: str) -> tuple[Fact, ...]:
        """Projection của KG: chỉ edge có predicate cho trước.

        Đây là cách API/Network/Ownership/Business graph ra đời — không phải đồ
        thị riêng, mà là lát cắt của cùng một Knowledge Graph.
        """
        wanted = frozenset(predicates)
        return tuple(e for e in self.edges if e.predicate in wanted)

    def nodes_of_type(self, prefix: str) -> frozenset[str]:
        """Mọi node thuộc một loại (theo tiền tố) xuất hiện trong graph."""
        scheme = f"{prefix}:"
        nodes: set[str] = set()
        for e in self.edges:
            for n in (e.subject, e.obj):
                if n.startswith(scheme):
                    nodes.add(n)
        return frozenset(nodes)

    @property
    def known_nodes(self) -> frozenset[str]:
        """Node ĐÃ QUAN SÁT được (mọi loại), không chỉ suy từ edge.

        Bằng chứng quan sát = là subject của một Fact THUỘC TÍNH (predicate phi
        quan hệ, vd exposes_port/runs_service trên host); cộng service quan sát
        chạy thật (obj của runs_service → node 'svc:'). Việc một node chỉ xuất
        hiện làm đầu/đuôi của EDGE không chứng minh nó tồn tại.
        """
        attr_subjects = {
            f.subject for f in self.facts if f.predicate not in RELATIONAL_PREDICATES
        }
        observed_services = {
            f"svc:{f.obj}" for f in self.facts if f.predicate == "runs_service"
        }
        return frozenset(attr_subjects | observed_services)

    @property
    def unknown_edge_targets(self) -> frozenset[str]:
        """Edge trỏ tới node CHƯA quan sát được → câu hỏi kiến trúc (Interview sau).

        Never assume: AI biết "payment reads db:orders" nhưng chưa từng thấy
        db:orders → đánh dấu Unknown, KHÔNG bịa nó tồn tại. Tổng quát mọi loại node.
        """
        targets = {e.obj for e in self.edges}
        known = self.known_nodes
        return frozenset(t for t in targets if t not in known)
