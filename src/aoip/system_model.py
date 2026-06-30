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

from aoip.objects import Fact


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
