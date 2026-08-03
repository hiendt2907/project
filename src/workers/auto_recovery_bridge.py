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
import os
from typing import Any

logger = logging.getLogger(__name__)

_SUPPORTED_CAPABILITIES = frozenset({
    "systemd.restart_unit", "systemd.reset_failed", "systemd.journal_vacuum",
})
_MIN_DISPATCH_CONFIDENCE = 0.75
_AUTO_APPROVER = "auto-recovery:diagnosis_loop"
_DISPATCH_TIMEOUT_S = 15.0

# Blast-radius allowlist for UNATTENDED dispatch (no human in the loop).
#
# Read straight from the environment rather than WorkerSettings, mirroring the
# gateway's own _master_auto_execute_enabled() convention, so this guard cannot
# be bypassed by a settings object a caller constructs itself in a test/harness.
#
# Why an explicit agent list and NOT OMNI_ENV_MODE: the two processes on this
# closed loop disagree about what environment they are in. Measured 2026-08-02:
# omni-gateway runs OMNI_ENV_MODE=prod while omni-fullstack runs
# OMNI_ENV_MODE=dev. Gating on env_mode would therefore mean "lab" evaluates
# differently depending on which half of the loop asks — the exact class of
# split-brain that let the master kill-switch sit forgotten at true for three
# weeks (docs/post-mortems/drift-correction-2026-07-02.md). An agent_id list is
# unambiguous in both processes and names the blast radius directly.
#
# Empty/unset => NO agent may be auto-executed against. This is the code
# default and it is fail-closed: a production deployment that never sets this
# variable inherits nothing, no matter how the tier/kill-switch/tenant toggle
# are configured.
_ENV_LAB_AGENTS = "OMNI_LAB_AUTO_EXECUTE_AGENTS"


def lab_auto_execute_agents(env: Any = None) -> frozenset[str]:
    """Parse the unattended-dispatch agent allowlist. Empty set = nothing allowed."""
    env = os.environ if env is None else env
    raw = str(env.get(_ENV_LAB_AGENTS, "") or "").strip()
    return frozenset(a.strip() for a in raw.split(",") if a.strip())


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


# Bounded work-list the outcome loop drains. A ZSET of the commands this worker
# actually dispatched — NOT a KEYS/SCAN over omni:cmd:rec:* — so closing the loop
# costs O(dispatched) and never walks another tenant's command space.
PENDING_KEY = "omni:autorecovery:pending"
_PENDING_TTL_S = 3600


def pending_member(tenant_id: str, command_id: str) -> str:
    return f"{tenant_id}|{command_id}"


async def register_pending_command(
    redis: Any, *, tenant_id: str, command_id: str, trace_id: str,
    agent_id: str, unit: str, capability: str,
) -> None:
    """Record a dispatched command so its terminal outcome can be reconciled back
    onto the originating trace. Best-effort: the mutation is already dispatched
    and already audited at this point, so a bookkeeping failure must not raise
    into the caller — it degrades observability, not safety."""
    import json
    import time

    try:
        now = int(time.time())
        await redis.zadd(PENDING_KEY, {pending_member(tenant_id, command_id): now})
        await redis.set(
            f"omni:autorecovery:meta:{tenant_id}:{command_id}",
            json.dumps({"trace_id": trace_id, "agent_id": agent_id, "unit": unit,
                        "capability": capability, "dispatched_at": now}),
            ex=_PENDING_TTL_S,
        )
    except Exception as exc:  # noqa: BLE001 — observability bookkeeping only
        logger.warning("event=auto_recovery_pending_register_failed command_id=%s err=%s",
                       command_id, exc)


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
    redis: Any = None, kafka: Any = None,
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

    # Checked after the cheap expected-off guards above so that a deployment with
    # auto-recovery simply not configured keeps reporting that, rather than being
    # relabelled as an allowlist rejection.
    if agent_id not in lab_auto_execute_agents():
        logger.info(
            "event=auto_recovery_skipped reason=agent_not_in_lab_allowlist trace=%s agent=%s "
            "unit=%s — add the agent_id to %s to permit unattended execution",
            trace_id, agent_id, suggested["unit"], _ENV_LAB_AGENTS,
        )
        return {"dispatched": False, "reason": "agent_not_in_lab_allowlist",
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

    # ── CRAT fail-closed: the mutation must be in the signed ledger BEFORE it is
    # dispatched, never after. An unattended host mutation with no audit block is
    # exactly what write_audit_block() exists to make impossible, so a ledger
    # failure aborts the dispatch rather than degrading to "execute and hope".
    # redis/kafka are optional only so the pure unit tests of the eligibility
    # rules above can call this without a full worker context; when they are
    # absent no dispatch happens at all (fail-closed, not silently unaudited).
    if redis is None or kafka is None:
        logger.error(
            "event=auto_recovery_skipped reason=audit_ledger_unavailable trace=%s agent=%s "
            "— refusing to dispatch an unaudited mutation", trace_id, agent_id,
        )
        return {"dispatched": False, "reason": "audit_ledger_unavailable",
                "command_id": None, "state": None}

    from services.audit_ledger.chain_writer import AuditLedgerError, write_audit_block
    from services.audit_ledger.crat_event_types import CRAT_EVENT_ADVISORY_DISPATCHED

    try:
        await write_audit_block(
            event_type=CRAT_EVENT_ADVISORY_DISPATCHED,
            trace_id=trace_id,
            payload={
                "source": "auto_recovery_bridge",
                "agent_id": agent_id,
                "tenant_id": tenant_id,
                "capability": suggested["capability"],
                "unit": suggested["unit"],
                "command_id": command["command_id"],
                "action_id": command.get("action_id", ""),
                "canonical_scope": command.get("canonical_scope", ""),
                "payload_hash": command.get("payload_hash", ""),
                "approver": _AUTO_APPROVER,
                "confidence": confidence,
                "root_cause": str(final.get("root_cause") or "")[:500],
                "unattended": True,
            },
            redis=redis,
            kafka=kafka,
            kafka_topic=getattr(settings, "kafka_topic_audit_chain", "omni-audit-chain"),
            tenant_id=tenant_id,
        )
    except AuditLedgerError as exc:
        logger.critical(
            "event=audit_chain_write_failed phase=auto_recovery_dispatch trace=%s agent=%s "
            "command_id=%s err=%s FAIL_CLOSED — mutation NOT dispatched",
            trace_id, agent_id, command["command_id"], exc,
        )
        return {"dispatched": False, "reason": f"crat_write_failed: {exc}",
                "command_id": command["command_id"], "state": None}

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
    if resp.status_code == 200:
        await register_pending_command(
            redis, tenant_id=tenant_id, command_id=command["command_id"],
            trace_id=trace_id, agent_id=agent_id, unit=suggested["unit"],
            capability=suggested["capability"],
        )
    return {
        "dispatched": resp.status_code == 200, "reason": "dispatched",
        "command_id": command["command_id"], "state": body.get("state"),
        "http_status": resp.status_code, "gateway_detail": body if resp.status_code != 200 else None,
    }
