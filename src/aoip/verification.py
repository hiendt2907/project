"""Domain-neutral verification contract for every remote mutation.

Verification is deliberately separate from transport/executor details.  A
domain adapter may verify a systemd service, a Kubernetes rollout, a database
replica or a network route, but Omni receives the same PASS/FAIL/UNKNOWN shape.

Confidence-axis boundary (Phase 3, ``docs/architecture/
CONFIDENCE_AXES_BOUNDARY.md``): ``VerificationResult`` answers "did THIS
mutation attempt reach its expected_state, right now?" — one-shot, transient,
created per ``RecoveryOutcome`` and only ever *audit-logged* (``to_dict()``
consumed by ``systemd_restart.py``/``systemd_reset_failed.py``/
``systemd_journal_vacuum.py`` for the CRAT event payload), never re-derived
later as "current state". This is a DIFFERENT axis from ``aoip.
competency_matrix.FacetState``, which answers "how much do we know about
facet Y of entity X, accumulated over time" as a pure derived projection
(``INV_DERIVED_NEVER_PERSIST``) recomputed from persisted Facts/Claims on
every read. Do not conflate the two just because both carry
``evidence_refs``/``confidence`` fields — see the boundary doc for the
field-by-field comparison and the "which one applies" decision table.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class VerificationStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: VerificationStatus
    expected_state: str
    evidence_refs: tuple[str, ...]
    checks: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.expected_state.strip():
            raise ValueError("expected_state is required")
        if not self.evidence_refs:
            raise ValueError("verification evidence_refs are required")
        if any(not str(ref).strip() for ref in self.evidence_refs):
            raise ValueError("verification evidence_refs must be non-empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("verification confidence must be between 0 and 1")
        if self.status is VerificationStatus.UNKNOWN and not self.reason.strip():
            raise ValueError("UNKNOWN verification requires a reason")

    @property
    def is_success(self) -> bool:
        return self.status is VerificationStatus.PASS

    @classmethod
    def pass_(cls, *, expected_state: str, evidence_refs: tuple[str, ...],
              checks: dict[str, Any] | None = None, confidence: float = 1.0) -> "VerificationResult":
        return cls(VerificationStatus.PASS, expected_state, evidence_refs,
                   checks or {}, confidence)

    @classmethod
    def fail(cls, *, expected_state: str, evidence_refs: tuple[str, ...],
             checks: dict[str, Any] | None = None, reason: str = "",
             confidence: float = 1.0) -> "VerificationResult":
        return cls(VerificationStatus.FAIL, expected_state, evidence_refs,
                   checks or {}, confidence, reason)

    @classmethod
    def unknown(cls, *, expected_state: str, reason: str,
                evidence_refs: tuple[str, ...]) -> "VerificationResult":
        return cls(VerificationStatus.UNKNOWN, expected_state, evidence_refs,
                   {}, 0.0, reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "expected_state": self.expected_state,
            "evidence_refs": list(self.evidence_refs),
            "checks": dict(self.checks),
            "confidence": self.confidence,
            "reason": self.reason,
        }


__all__ = ["VerificationResult", "VerificationStatus"]
