"""Advisory Mode HITL Compatibility — deprecate omni-hitl-pending; route to Telegram for human acknowledgment."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class AdvisoryHITLCompat:
    """
    In Advisory Mode (Phase 5), mutations are not executed autonomously.
    Previously: Alert → HITL Dispatcher → omni-hitl-pending → Human approves → Executor mutates
    Now: Alert → Advisory Analyst → Telegram (human reviews) → manual execution (not via omni-actions)

    This module prevents accidental emission of omni-hitl-pending and ensures all suggestions
    flow through Telegram + SUGGEST_REMEDIATION (non-mutating).
    """

    # HITL is DISABLED in advisory mode
    OMNI_HITL_ENABLED = False

    @staticmethod
    def validate_hitl_gate(
        trace_id: str,
        context: str = "unknown",
        settings: Any | None = None,
    ) -> tuple[bool, str]:
        """
        Pre-HITL validation. Returns (allow_hitl, reason).

        HITL is enabled only when OMNI_HITL_ROUTING_ENABLED=true in settings.
        Default (and legacy) behavior: HITL blocked (advisory mode, suggest-only).

        Args:
            trace_id: Trace ID for logging
            context: Where HITL was requested (analyst, planner, etc.)
            settings: WorkerSettings object; reads omni_hitl_routing_enabled when present.

        Returns:
            (True, "") when HITL routing is enabled via settings.
            (False, reason_message) otherwise.
        """
        hitl_routing_enabled = bool(
            getattr(settings, "omni_hitl_routing_enabled", False)
        ) if settings is not None else False

        if hitl_routing_enabled:
            logger.info(
                "event=advisory_hitl_gate_open trace=%s context=%s",
                trace_id,
                context,
            )
            return True, ""

        if not AdvisoryHITLCompat.OMNI_HITL_ENABLED:
            reason = (
                f"ADVISORY_MODE_HITL_DISABLED: Mutation approval via HITL blocked. "
                f"Advisory Mode routes suggestions through Telegram only. "
                f"(context={context})"
            )
            logger.warning(
                "event=advisory_hitl_blocked trace=%s context=%s",
                trace_id,
                context,
            )
            return False, reason
        return True, ""

    @staticmethod
    def emit_advisory_suggestion_to_telegram(
        trace_id: str,
        verdict: str,
        root_cause: str,
        proposed_actions: list[dict[str, Any]],
        verification_steps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """
        Build a Telegram-ready advisory suggestion (instead of omni-hitl-pending).

        Returns: suggestion dict suitable for render_advisory_to_telegram()
        """
        return {
            "trace_id": trace_id,
            "verdict": verdict,
            "root_cause": root_cause,
            "proposed_remediation": proposed_actions,
            "verification_steps": verification_steps or [],
            "forecast": {
                "method": "human_review",
                "basis": "Advisory mode — human decision required",
                "forecasts": [],
            },
            "escalation_reason": "Advisory Mode: All suggestions require manual execution",
        }
