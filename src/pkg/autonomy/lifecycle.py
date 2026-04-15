"""Alert lifecycle phases vs internal autonomy transitions (audit / dashboards).

Keep transition string literals in sync with ``workers.autonomy_contract``.
"""

from __future__ import annotations

from enum import Enum

# Mirror workers.autonomy_contract (no import from workers → pkg stays import-clean).
TRANSITION_INGESTED = "INGESTED"
TRANSITION_CONTEXT_READY = "CONTEXT_READY"
TRANSITION_DIAGNOSED = "DIAGNOSED"
TRANSITION_PLAN_EMITTED = "PLAN_EMITTED"
TRANSITION_EXECUTED = "EXECUTED"
TRANSITION_VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
TRANSITION_REQUIRES_HUMAN = "REQUIRES_HUMAN"


class AlertPhase(str, Enum):
    """User-facing state machine: Pending → Triage → Action → Verify → Done / Escalated."""

    PENDING = "pending"  # received, not yet contextualized
    TRIAGE = "triage"  # evidence + diagnosis (CONTEXT_READY / DIAGNOSED)
    ACTION = "action"  # plan emitted or mutate in flight
    VERIFY = "verify"  # post-exec SDK / probe verify
    DONE = "done"  # VERIFIED_SUCCESS → optional VectorDB learn
    ESCALATED = "escalated"  # REQUIRES_HUMAN or terminal failure


_TRANSITION_TO_PHASE: dict[str, AlertPhase] = {
    TRANSITION_INGESTED: AlertPhase.PENDING,
    TRANSITION_CONTEXT_READY: AlertPhase.TRIAGE,
    TRANSITION_DIAGNOSED: AlertPhase.TRIAGE,
    TRANSITION_PLAN_EMITTED: AlertPhase.ACTION,
    TRANSITION_EXECUTED: AlertPhase.VERIFY,
    TRANSITION_VERIFIED_SUCCESS: AlertPhase.DONE,
    TRANSITION_REQUIRES_HUMAN: AlertPhase.ESCALATED,
}


def transition_to_alert_phase(transition: str) -> AlertPhase:
    """Map Kafka/log ``transition=…`` to a coarse ``AlertPhase``."""
    t = str(transition or "").strip()
    return _TRANSITION_TO_PHASE.get(t, AlertPhase.PENDING)
