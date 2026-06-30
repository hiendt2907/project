"""Ontology objects (Runtime + Derived) cho walking skeleton.

Chỉ hiện thực các object đã khai báo trong META_MODEL/SEMANTIC_RULES — KHÔNG noun
mới (INV_NO_NEW_NOUNS). Runtime object bất biến; chuyển trạng thái bằng
``dataclasses.replace`` (immutable, INV trong coding-style).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum


def _now() -> float:
    return time.time()


# ── Action lifecycle (SEMANTIC_RULES Appendix A.1) ───────────────────────────
class ActionState(str, Enum):
    PLANNED = "planned"
    VALIDATED = "validated"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    ABORTED = "aborted"


TERMINAL_ACTION_STATES = frozenset(
    {ActionState.COMPLETED, ActionState.ROLLED_BACK, ActionState.ABORTED, ActionState.FAILED}
)


# ── Runtime objects (immutable) ──────────────────────────────────────────────
@dataclass(frozen=True)
class Observation:
    """Raw, immutable, transient (Knowledge §Q1)."""

    source: str
    scope: str
    data: dict
    ts: float = field(default_factory=_now)


@dataclass(frozen=True)
class Hypothesis:
    """Diễn giải chưa chắc; mang prior + predicted_evidence (Cognitive Q1)."""

    claim: str
    predicted_evidence: tuple[str, ...]
    prior: float
    origin: str  # EXPERIENCE | DECISION_GRAPH | TEMPORAL | TOPOLOGY


@dataclass(frozen=True)
class Finding:
    """Kết luận đã đối chiếu; immutable; references observation (SEMANTIC §2)."""

    claim: str
    references: tuple[str, ...]
    verdict: bool
    confidence: float


@dataclass(frozen=True)
class Decision:
    """consumes ≥1 Finding + Capability/Authority; produces Action (SEMANTIC §2)."""

    goal: str
    scope: str
    consumes: tuple[str, ...]  # finding claims (explainability)


@dataclass(frozen=True)
class Fact:
    """Tri thức ĐÃ VERIFY (Knowledge §Q1): bitemporal + provenance + confidence.

    Là đỉnh của vòng tiến hóa Observation→Hypothesis→Fact (Cognitive Model). Chỉ
    hypothesis đã được Verify mới trở thành Fact. Bất biến; supersede bằng Fact mới.
    """

    subject: str  # entity, vd "host:web-01"
    predicate: str  # quan hệ, vd "exposes_port" / "runs_service"
    obj: str  # giá trị, vd "6379" / "redis"
    confidence: float
    provenance: tuple[str, ...]  # observation sources (Provenance chain)
    observation_time: float = field(default_factory=_now)
    verified_time: float = field(default_factory=_now)

    @property
    def triple(self) -> tuple[str, str, str]:
        return (self.subject, self.predicate, self.obj)


@dataclass(frozen=True)
class Communication:
    """Communication node (Knowledge/Org Model): runtime hỏi người khi gặp Unknown.

    Hiện thực CRITICAL RULE của MASTER_PLAN: "Never assume" — thay vì hallucinate,
    sinh câu hỏi có cấu trúc cho con người (INV_HUMAN_ACCOUNTABILITY).
    """

    question: str
    scope: str
    blocking_unknown: str  # điều runtime KHÔNG xác định được
    options: tuple[str, ...] = ()
    ts: float = field(default_factory=_now)


@dataclass(frozen=True)
class Action:
    """implements đúng 1 Decision; recoverable. Chuyển state bằng replace()."""

    decision_goal: str
    scope: str
    plan: str
    state: ActionState = ActionState.PLANNED
    result: dict = field(default_factory=dict)

    def at(self, state: ActionState, **result) -> "Action":
        merged = {**self.result, **result} if result else self.result
        return replace(self, state=state, result=merged)
