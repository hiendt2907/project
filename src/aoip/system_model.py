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

    @property
    def _observed_service_nodes(self) -> frozenset[str]:
        """Service đã QUAN SÁT chạy thật (từ Fact runs_service) ở dạng node 'svc:'."""
        return frozenset(f"svc:{f.obj}" for f in self.facts if f.predicate == "runs_service")

    @property
    def unknown_edge_targets(self) -> frozenset[str]:
        """Edge trỏ tới service CHƯA quan sát được → câu hỏi kiến trúc (Interview sau).

        Never assume: AI biết "payment depends_on analytics" nhưng chưa từng thấy
        'analytics' chạy → đánh dấu Unknown, KHÔNG bịa nó tồn tại.
        """
        # "Đã biết" = service quan sát chạy thật (runs_service). Việc một node xuất
        # hiện làm SUBJECT của edge KHÔNG chứng minh nó tồn tại (vẫn có thể là suy
        # diễn từ config) → không tính là observed.
        observed = self._observed_service_nodes
        targets = {e.obj for e in self.edges}
        return frozenset(
            t for t in targets if t not in observed and not t.startswith("host:")
        )
