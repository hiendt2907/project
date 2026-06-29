"""CapabilityState + Assessment recompute (Capability Model + ASSESSMENT.md).

CapabilityScore = Π(dimensions) — Derived, recompute (INV_DERIVED_NEVER_PERSIST,
INV_CAPABILITY_IS_PRODUCT). Assess cập nhật chiều E (Execution) từ evidence
(Finding outcome) — vòng phản hồi INV_ASSESSMENT_CLOSES_LOOP.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from math import prod

DIMENSIONS = ("K", "R", "E", "C", "G", "L")


class Maturity(str, Enum):
    NASCENT = "nascent"
    DEVELOPING = "developing"
    PROVEN = "proven"
    DEGRADED = "degraded"


def _maturity_from(evidence_count: int) -> Maturity:
    if evidence_count == 0:
        return Maturity.NASCENT
    if evidence_count < 3:
        return Maturity.DEVELOPING
    return Maturity.PROVEN


@dataclass(frozen=True)
class CapabilityState:
    """Runtime state per (capability, scope). score là Derived, không lưu làm truth."""

    capability_id: str
    scope: str
    dimensions: dict[str, float] = field(
        default_factory=lambda: {d: (1.0 if d in ("K", "R", "C", "G", "L") else 0.0) for d in DIMENSIONS}
    )
    evidence: tuple[bool, ...] = ()  # outcome history (Finding.verdict)

    @property
    def score(self) -> float:
        """Derived: Π(dimensions). Chiều yếu nhất kéo xuống."""
        return prod(self.dimensions.values())

    @property
    def maturity(self) -> Maturity:
        return _maturity_from(len(self.evidence))


def assess(state: CapabilityState, outcome: bool) -> CapabilityState:
    """Cập nhật chiều E từ evidence mới rồi recompute (Assess = vòng phản hồi).

    E = tỉ lệ thành công có trọng số recency (đơn giản: success_rate trên evidence).
    """
    evidence = state.evidence + (outcome,)
    success_rate = sum(1 for o in evidence if o) / len(evidence)
    dims = {**state.dimensions, "E": round(success_rate, 4)}
    return replace(state, dimensions=dims, evidence=evidence)
