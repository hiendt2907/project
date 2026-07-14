"""Grounded advisory → canonical durable mutation command bridge.

This is the provider-side seam between diagnosis/decision and the agent runtime.
It only builds a typed envelope; the Gateway still owns durable enqueue and the
agent still owns local preflight/execution.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from aoip.capabilities.systemd_restart import build_typed_payload, issue_capability_command
from aoip.command_contract import canonical_payload_hash
from aoip.objects import Finding


def build_durable_command(
    advisory: dict[str, Any], *, tenant: str, agent_id: str, approver: str,
    now: float | None = None, ttl_s: int = 300,
) -> dict[str, Any]:
    """Turn one grounded advisory into a ready-to-enqueue durable command.

    The initial bridge intentionally supports only the existing typed
    ``systemd.restart_unit`` capability. Unsupported domains must add their own
    typed capability instead of falling back to a raw shell command.
    """
    if not tenant.strip() or not agent_id.strip() or not approver.strip():
        raise ValueError("tenant, agent_id and approver are required")
    required = ("mission_id", "decision_id", "incident_id", "evidence_refs")
    missing = [key for key in required if not advisory.get(key)]
    if missing:
        raise ValueError(f"grounded advisory required: {missing}")
    if advisory.get("capability") != "systemd.restart_unit":
        raise ValueError("unsupported capability: use a typed domain adapter")
    confidence = float(advisory.get("confidence", 0.0))
    if confidence < 0.5:
        raise ValueError("diagnosis confidence below mutation threshold")
    unit = str(advisory.get("unit") or "").strip()
    if not unit:
        raise ValueError("grounded advisory required: unit")
    if ttl_s <= 0:
        raise ValueError("ttl_s must be positive")

    issued_at = time.time() if now is None else now
    typed = build_typed_payload(
        mission_id=str(advisory["mission_id"]),
        decision_id=str(advisory["decision_id"]),
        incident_id=str(advisory["incident_id"]),
        summary=str(advisory.get("summary") or advisory.get("diagnosis") or ""),
        unit=unit,
    )
    typed["reason"]["diagnosed_at"] = issued_at
    refs = tuple(str(ref) for ref in advisory["evidence_refs"] if str(ref).strip())
    finding = Finding(
        claim=f"svc:{unit} is DOWN ({typed['reason']['summary']})",
        references=refs,
        verdict=True,
        confidence=confidence,
    )
    payload = issue_capability_command(
        typed_payload=typed,
        approver=approver,
        tenant=tenant,
        issued_at=issued_at,
        expires_at=issued_at + ttl_s,
        findings=(finding,),
        diagnosis_confidence=confidence,
    )
    command_id = f"cmd-{uuid.uuid4().hex[:16]}"
    return {
        "command_id": command_id,
        "agent_id": agent_id,
        "tenant_id": tenant,
        "mission_id": advisory["mission_id"],
        "incident_id": advisory["incident_id"],
        "decision_id": advisory["decision_id"],
        "action_id": payload["approval"]["action_id"],
        "canonical_scope": payload["approval"]["canonical_scope"],
        "payload_hash": canonical_payload_hash(payload),
        "payload": payload,
        "ttl_s": ttl_s,
    }


__all__ = ["build_durable_command"]
