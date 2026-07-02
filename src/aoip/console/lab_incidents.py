"""Provider lab incident/proposal projection for M2 controlled loop.

This module creates the first product-visible runtime object for a controlled
lab operation: incident + diagnosis + typed action proposal. It deliberately
stops before durable command enqueue; approval and execution are later links.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from aoip.agent.trace import EV_COMMAND_RECEIVED, Correlation, RuntimeTrace, canonical_scope

OPERATION_TYPE = "systemd.restart_unit"
_OPS_INDEX = "omni:aoip:ops:{tenant_id}"
_OP_KEY = "omni:aoip:op:{tenant_id}:{correlation_id}"


def _stable_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


async def _load_agent(redis: Any, agent_id: str) -> dict[str, Any] | None:
    raw = await redis.get(f"omni:remote_agent:registry:{agent_id}")
    return json.loads(raw) if raw else None


async def _evidence_refs(redis: Any, agent_id: str, service: str) -> list[str]:
    refs = [f"agent:{agent_id}"]
    checks = await redis.hgetall(f"omni:remote_agent:checks:{agent_id}")
    service_l = service.lower()
    for probe, raw in checks.items():
        try:
            item = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        haystack = f"{probe} {item.get('alert_hint', '')}".lower()
        if service_l in haystack or service_l.replace(".service", "") in haystack:
            refs.append(f"check:{probe}:{item.get('result', 'UNKNOWN')}")
    return refs


def _proposed_action(*, tenant_id: str, agent_id: str, unit: str, service: str,
                     incident_id: str, mission_id: str, decision_id: str,
                     approval_id: str) -> dict[str, Any]:
    idem = _stable_hash(tenant_id, agent_id, unit, incident_id, OPERATION_TYPE)
    return {
        "operation_type": OPERATION_TYPE,
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "target": {"unit": unit},
        "reason": decision_id,
        "incident_id": incident_id,
        "mission_id": mission_id,
        "idempotency_key": idem,
        "approval_id": approval_id,
        "expected_precondition": {"unit_exists": True},
        "verification": {"active_state": "active", "sub_state": "running"},
        "service": service,
    }


async def create_lab_incident(
    redis: Any, *, tenant_id: str, agent_id: str, host: str, service: str, unit: str,
    requested_by: str, now: float | None = None,
) -> dict[str, Any]:
    """Create a lab incident proposal. No mutation, no durable command enqueue."""
    now = time.time() if now is None else now
    agent = await _load_agent(redis, agent_id)
    if agent is None:
        raise ValueError("agent_not_found")
    if agent.get("tenant_id") != tenant_id:
        raise ValueError("agent_tenant_mismatch")

    seed = _stable_hash(tenant_id, agent_id, unit, str(int(now * 1000)))
    incident_id = f"lab-{service}-{seed[:8]}"
    mission_id = f"mis-{seed[:12]}"
    decision_id = f"dec-{seed[:12]}"
    action_id = f"act-{seed[:12]}"
    command_id = f"cmd-{seed[:12]}"
    approval_id = f"appr-{seed[:12]}"
    corr = Correlation(
        tenant=tenant_id, agent_id=agent_id, mission_id=mission_id,
        incident_id=incident_id, decision_id=decision_id, action_id=action_id,
        command_id=command_id, canonical_scope=canonical_scope(tenant_id, f"svc:{service}"),
    )

    evidence = await _evidence_refs(redis, agent_id, service)
    diagnosis = {
        "claim": f"{service} on {host} is eligible for controlled lab restart",
        "confidence": 0.8 if len(evidence) > 1 else 0.55,
        "evidence_refs": evidence,
        "capability": OPERATION_TYPE,
    }
    action = _proposed_action(
        tenant_id=tenant_id, agent_id=agent_id, unit=unit, service=service,
        incident_id=incident_id, mission_id=mission_id, decision_id=decision_id,
        approval_id=approval_id,
    )
    caps = set(agent.get("capabilities") or ())
    has_capability = OPERATION_TYPE in caps
    status = "PENDING_APPROVAL" if has_capability else "BLOCKED_AGENT_CAPABILITY"
    block_reason = "" if has_capability else (
        f"agent {agent_id} does not advertise {OPERATION_TYPE}; capabilities={sorted(caps)}"
    )

    record = {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "host": host,
        "service": service,
        "unit": unit,
        "incident_id": incident_id,
        "mission_id": mission_id,
        "decision_id": decision_id,
        "action_id": action_id,
        "command_id": command_id,
        "approval_id": approval_id,
        "correlation_id": corr.correlation_id,
        "canonical_scope": corr.canonical_scope,
        "status": status,
        "block_reason": block_reason,
        "diagnosis": diagnosis,
        "proposed_action": action,
        "requested_by": requested_by,
        "created_at": now,
    }

    trace = RuntimeTrace(redis)
    await trace.emit(
        EV_COMMAND_RECEIVED, corr, state_before="", state_after="diagnosed",
        reason=f"lab incident created; proposed {OPERATION_TYPE} for {unit}",
        evidence_refs=tuple(evidence), ts=now, source_version=1,
    )
    if has_capability:
        await trace.mark_pending_approval(corr, reason=f"Approve {OPERATION_TYPE} {unit}", ts=now)

    await redis.set(_OP_KEY.format(tenant_id=tenant_id, correlation_id=corr.correlation_id),
                    json.dumps(record), ex=7 * 86400)
    await redis.sadd(_OPS_INDEX.format(tenant_id=tenant_id), corr.correlation_id)
    return record


async def list_provider_lab_incidents(redis: Any, trace: RuntimeTrace) -> dict[str, Any]:
    """List operation proposals joined with RuntimeTrace projection."""
    out: list[dict[str, Any]] = []
    for key in sorted(await redis.keys(_OPS_INDEX.format(tenant_id="*"))):
        tenant_id = str(key).split("omni:aoip:ops:", 1)[1]
        for correlation_id in sorted(await redis.smembers(key)):
            raw = await redis.get(_OP_KEY.format(tenant_id=tenant_id, correlation_id=correlation_id))
            if not raw:
                continue
            record = json.loads(raw)
            events = await trace.timeline(tenant_id, correlation_id)
            record["event_count"] = len(events)
            record["current_phase"] = events[-1]["event_type"] if events else "unknown"
            out.append(record)
    out.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"incidents": out}
