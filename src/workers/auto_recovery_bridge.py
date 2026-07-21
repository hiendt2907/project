"""Phase 4 (0-6 roadmap) — the actual closed loop: diagnosis -> automated
recovery dispatch, gated by tier_gate. Zero manually authored JSON.

Bridges services/analyst/diagnosis_loop.py's LLM-produced finding to
aoip.command_bridge.build_durable_command(), then enqueues it against the
real gateway over HTTP (worker and gateway are separate pods — this is a
real network call, not a function call, matching command_bridge.py's own
"provider-side seam" framing).

Fail-closed by design at every step:
  - No suggested_recovery in the diagnosis -> no dispatch (the overwhelming
    majority of diagnoses; most incidents are not a known, safely-automatable
    capability).
  - Confidence below threshold -> no dispatch.
  - OMNI_GATEWAY_API_KEY unset -> no dispatch (this module never silently
    no-ops without saying so — see dispatch_if_eligible()'s return reason).
  - The gateway's own gates (tenant mutation toggle, master kill-switch,
    tier_gate from Phase 2) still apply on every dispatch attempt — this
    module does not and cannot bypass them; it is just another (automated)
    caller of the same durable enqueue endpoint the operator CLI uses.

This module does NOT decide policy (what tier, what risk is auto-allowed) —
that is entirely the gateway's job (Phase 2). It only decides WHETHER a
diagnosis maps to a known, structured, dispatchable capability at all.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_SUPPORTED_CAPABILITIES = frozenset({"systemd.restart_unit"})
_MIN_DISPATCH_CONFIDENCE = 0.75
_AUTO_APPROVER = "auto-recovery:diagnosis_loop"
_DISPATCH_TIMEOUT_S = 15.0


def extract_suggested_recovery(final: dict[str, Any]) -> dict[str, str] | None:
    """Pull a structured {"capability", "unit"} out of a diagnosis session's
    "final" dict, if present and well-formed. Returns None for anything else
    — this is the ONLY place that decides "is this diagnosis dispatchable",
    everything downstream trusts its output."""
    suggested = final.get("suggested_recovery")
    if not isinstance(suggested, dict):
        return None
    capability = str(suggested.get("capability", "")).strip()
    unit = str(suggested.get("unit", "")).strip()
    if capability not in _SUPPORTED_CAPABILITIES or not unit:
        return None
    # The LLM copies the unit verbatim from evidence facts (bare name, e.g.
    # "payment-api" — grounding validates against that exact string) but the
    # executor's AOIP_ALLOWED_SYSTEMD_UNITS allowlist does an EXACT match
    # against the full systemd unit name (e.g. "payment-api.service").
    # Normalize here, downstream of grounding, so neither side has to agree
    # on a suffix convention.
    if not unit.endswith(".service"):
        unit = f"{unit}.service"
    return {"capability": capability, "unit": unit}


def build_dispatch_advisory(
    *, final: dict[str, Any], suggested: dict[str, str],
    mission_id: str, decision_id: str, incident_id: str,
) -> dict[str, Any]:
    """Shape a diagnosis session's final result into the advisory dict
    aoip.command_bridge.build_durable_command() expects."""
    root_cause = str(final.get("root_cause") or "")
    confidence = float(final.get("confidence", 0.0) or 0.0)
    return {
        "mission_id": mission_id, "decision_id": decision_id, "incident_id": incident_id,
        "capability": suggested["capability"], "unit": suggested["unit"],
        "summary": root_cause or f"{suggested['unit']} identified for recovery by diagnosis loop",
        "confidence": confidence,
        "evidence_refs": [f"diagnosis_session:{incident_id}"],
    }


async def dispatch_if_eligible(
    *, settings: Any, http_client: Any, final: dict[str, Any],
    agent_id: str, tenant_id: str, trace_id: str,
) -> dict[str, Any]:
    """Attempt automated dispatch for one diagnosis session. Always returns a
    result dict — never raises for an ineligible/skipped diagnosis (that is
    the expected, common case, not an error). Only network/gateway failures
    during an ELIGIBLE dispatch attempt propagate as exceptions.

    Result shape: {"dispatched": bool, "reason": str, "command_id": str|None,
    "state": str|None} — reason is always populated, even when dispatched
    is True (states why: "confidence_below_threshold", "no_suggested_recovery",
    "gateway_api_key_not_configured", "dispatched").
    """
    suggested = extract_suggested_recovery(final)
    if suggested is None:
        return {"dispatched": False, "reason": "no_suggested_recovery",
                "command_id": None, "state": None}

    confidence = float(final.get("confidence", 0.0) or 0.0)
    if confidence < _MIN_DISPATCH_CONFIDENCE:
        return {"dispatched": False, "reason": "confidence_below_threshold",
                "command_id": None, "state": None}

    api_key = getattr(settings, "omni_gateway_api_key", "") or ""
    if not api_key:
        logger.warning(
            "event=auto_recovery_skipped reason=gateway_api_key_not_configured "
            "trace=%s agent=%s — set OMNI_GATEWAY_API_KEY to enable auto-dispatch",
            trace_id, agent_id,
        )
        return {"dispatched": False, "reason": "gateway_api_key_not_configured",
                "command_id": None, "state": None}

    from aoip.command_bridge import build_durable_command

    advisory = build_dispatch_advisory(
        final=final, suggested=suggested,
        mission_id=f"mis-{trace_id}", decision_id=f"dec-{trace_id}", incident_id=trace_id,
    )
    try:
        command = build_durable_command(
            advisory, tenant=tenant_id, agent_id=agent_id, approver=_AUTO_APPROVER,
        )
    except ValueError as exc:
        logger.warning("event=auto_recovery_build_failed trace=%s err=%s", trace_id, exc)
        return {"dispatched": False, "reason": f"build_failed: {exc}",
                "command_id": None, "state": None}

    gateway_url = getattr(settings, "omni_gateway_internal_url", "").rstrip("/")
    resp = await http_client.post(
        f"{gateway_url}/webhook/agent/rt/commands/enqueue",
        json=command,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=_DISPATCH_TIMEOUT_S,
    )
    body = resp.json()
    logger.info(
        "event=auto_recovery_dispatched trace=%s agent=%s unit=%s command_id=%s state=%s http=%d",
        trace_id, agent_id, suggested["unit"], command["command_id"], body.get("state"), resp.status_code,
    )
    return {
        "dispatched": resp.status_code == 200, "reason": "dispatched",
        "command_id": command["command_id"], "state": body.get("state"),
        "http_status": resp.status_code, "gateway_detail": body if resp.status_code != 200 else None,
    }
