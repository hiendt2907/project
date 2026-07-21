"""Canonical correlation identity — the fields that should tie one incident's
diagnosis, decision, approval, and command together across every lane.

Every one of the six Command/CommandResult shapes found in the Phase 0 audit
carries some subset of these fields under slightly different names
(mission_id/incident_id/decision_id/action_id/command_id/tenant). None of
them enforces that a caller populates all of them. This matters concretely:
aoip.agent.operations._key_for() only uses a precise, per-command
idempotency key when every one of these is present on the decoded request —
otherwise it silently falls back to a coarser key
(tenant+scope+decision_goal+failure_mode+unit) that a later, unrelated
incident for the same target can collide with. See
docs/handoffs/PHASE_0_6_PROGRESS.md, Phase 0a, for a live instance of this.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CorrelationIdentity:
    tenant_id: str = ""
    mission_id: str = ""
    incident_id: str = ""
    decision_id: str = ""
    action_id: str = ""
    command_id: str = ""

    def is_fully_bound(self) -> bool:
        """True only when every correlation field is set.

        Mirrors the exact condition aoip.agent.operations._key_for() checks
        before it will use a correlation-based idempotency key — kept here
        as the canonical definition so future callers (and future lanes)
        can check "will this get a precise idempotency key?" without
        re-deriving the tuple by hand.
        """
        return all((
            self.tenant_id, self.mission_id, self.incident_id,
            self.decision_id, self.action_id, self.command_id,
        ))

    @classmethod
    def from_dict(cls, d: dict) -> "CorrelationIdentity":
        """Best-effort extraction from any of the six known shapes — reads
        whichever of these keys are present at the top level of `d`, missing
        ones default to "". Callers with a nested shape (e.g. AOIP's
        `reason`/`recovery` sub-dicts) should merge before calling this."""
        return cls(
            tenant_id=str(d.get("tenant_id") or d.get("tenant") or ""),
            mission_id=str(d.get("mission_id") or ""),
            incident_id=str(d.get("incident_id") or ""),
            decision_id=str(d.get("decision_id") or ""),
            action_id=str(d.get("action_id") or ""),
            command_id=str(d.get("command_id") or ""),
        )
