"""Provider Agents projection — read-only fleet view from runtime registry.

This is a product read-model for the Provider Portal. Source of truth remains
Redis keys written by the remote-agent/gateway/runtime paths; this module only
normalizes them into a table operators can read.
"""
from __future__ import annotations

import json
from typing import Any

_REMOTE_PREFIX = "omni:remote_agent:registry:"
_CHECKS_PREFIX = "omni:remote_agent:checks:"
_METRICS_PREFIX = "omni:remote_agent:metrics:"
_READY_PREFIX = "omni:cmd:ready:"

_ONLINE_SEC = 120
_STALE_SEC = 15 * 60


def _loads(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _status(age_seconds: int) -> str:
    if age_seconds <= _ONLINE_SEC:
        return "online"
    if age_seconds <= _STALE_SEC:
        return "stale"
    return "offline"


async def _latest_check(redis: Any, agent_id: str) -> dict[str, Any] | None:
    raw = await redis.hgetall(f"{_CHECKS_PREFIX}{agent_id}")
    if not raw:
        return None
    checks: list[dict[str, Any]] = []
    for probe, value in raw.items():
        item = _loads(value)
        if not item:
            continue
        try:
            ts = int(float(item.get("ts") or 0))
        except Exception:
            ts = 0
        checks.append({
            "probe": str(probe),
            "ts": ts,
            "result": str(item.get("result") or "UNKNOWN"),
            "summary": str(item.get("alert_hint") or item.get("summary") or ""),
        })
    if not checks:
        return None
    checks.sort(key=lambda c: c["ts"], reverse=True)
    return checks[0]


async def _command_state(redis: Any, tenant_id: str, agent_id: str) -> dict[str, Any]:
    ready_key = f"{_READY_PREFIX}{tenant_id}:{agent_id}"
    try:
        pending = int(await redis.zcard(ready_key))
    except Exception:
        pending = 0
    if pending > 0:
        return {"state": "active", "pending": pending}
    return {"state": "idle", "pending": 0}


async def build_provider_agents(redis: Any, *, now: float) -> dict[str, Any]:
    keys = await redis.keys(f"{_REMOTE_PREFIX}*")
    agents: list[dict[str, Any]] = []

    for key in sorted(keys):
        raw = await redis.get(key)
        rec = _loads(raw)
        if not rec:
            continue

        agent_id = str(rec.get("agent_id") or str(key).replace(_REMOTE_PREFIX, ""))
        tenant_id = str(rec.get("tenant_id") or "unknown")
        hostname = str(rec.get("hostname") or rec.get("host") or agent_id)
        last_seen = int(float(rec.get("last_seen") or 0))
        age = max(0, int(now - last_seen)) if last_seen else 10**9
        capabilities = [str(c) for c in (rec.get("capabilities") or [])]
        latest_check = await _latest_check(redis, agent_id)
        command = await _command_state(redis, tenant_id, agent_id)

        metrics = _loads(await redis.get(f"{_METRICS_PREFIX}{agent_id}"))
        agents.append({
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "hostname": hostname,
            "status": _status(age),
            "online": age <= _ONLINE_SEC,
            "age_seconds": age,
            "last_seen": last_seen,
            "version": str(rec.get("version") or "unknown"),
            "platform": str(rec.get("platform") or "unknown"),
            "capabilities": capabilities,
            "discovery_enabled": "discovery" in {c.lower() for c in capabilities},
            "evidence_count": int(rec.get("evidence_count") or 0),
            "last_discovery_result": latest_check,
            "command_state": command["state"],
            "pending_commands": command["pending"],
            "metrics": metrics or None,
        })

    summary = {
        "total": len(agents),
        "online": sum(1 for a in agents if a["status"] == "online"),
        "stale": sum(1 for a in agents if a["status"] == "stale"),
        "offline": sum(1 for a in agents if a["status"] == "offline"),
    }
    return {"generated_at": now, "summary": summary, "agents": agents}
