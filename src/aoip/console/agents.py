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
_RELEASE_MANIFEST_KEY = "omni:agent:release_manifest"

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


async def _load_release_manifest(redis: Any) -> dict[str, Any] | None:
    manifest = _loads(await redis.get(_RELEASE_MANIFEST_KEY))
    return manifest or None


def _classify_drift(rec: dict[str, Any], manifest: dict[str, Any] | None) -> str:
    """current | drifted | unknown — mirrors gateway/routes/agent_commands.py
    _classify_drift() so the provider portal and the admin API never disagree
    on the same registry record."""
    if not manifest or not manifest.get("bundle_sha256"):
        return "unknown"
    reported = str(rec.get("bundle_sha256") or "")
    if not reported:
        return "unknown"
    if reported != manifest.get("bundle_sha256") or rec.get("version") != manifest.get("version"):
        return "drifted"
    reported_aoip = str(rec.get("aoip_bundle_sha256") or "")
    if reported_aoip and reported_aoip != str(manifest.get("aoip_bundle_sha256") or ""):
        return "drifted"
    return "current"


async def _command_state(redis: Any, tenant_id: str, agent_id: str) -> dict[str, Any]:
    ready_key = f"{_READY_PREFIX}{tenant_id}:{agent_id}"
    try:
        pending = int(await redis.zcard(ready_key))
    except Exception:
        pending = 0
    if pending > 0:
        return {"state": "active", "pending": pending}
    return {"state": "idle", "pending": 0}


async def build_provider_agents(redis: Any, *, now: float,
                                tenant_id: str | None = None) -> dict[str, Any]:
    keys = await redis.keys(f"{_REMOTE_PREFIX}*")
    agents: list[dict[str, Any]] = []
    manifest = await _load_release_manifest(redis)
    tenant_filter = tenant_id

    for key in sorted(keys):
        raw = await redis.get(key)
        rec = _loads(raw)
        if not rec:
            continue

        agent_id = str(rec.get("agent_id") or str(key).replace(_REMOTE_PREFIX, ""))
        record_tenant_id = str(rec.get("tenant_id") or "unknown")
        if tenant_filter is not None and record_tenant_id != tenant_filter:
            continue
        hostname = str(rec.get("hostname") or rec.get("host") or agent_id)
        last_seen = int(float(rec.get("last_seen") or 0))
        age = max(0, int(now - last_seen)) if last_seen else 10**9
        capabilities = [str(c) for c in (rec.get("capabilities") or [])]
        latest_check = await _latest_check(redis, agent_id)
        command = await _command_state(redis, record_tenant_id, agent_id)

        metrics = _loads(await redis.get(f"{_METRICS_PREFIX}{agent_id}"))
        aoip_bundle_sha256 = str(rec.get("aoip_bundle_sha256") or "")
        agents.append({
            "agent_id": agent_id,
            "tenant_id": record_tenant_id,
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
            "runtime": "employee" if aoip_bundle_sha256 else "legacy",
            "bundle_sha256": str(rec.get("bundle_sha256") or ""),
            "aoip_bundle_sha256": aoip_bundle_sha256,
            "drift_status": _classify_drift(rec, manifest),
        })

    summary = {
        "total": len(agents),
        "online": sum(1 for a in agents if a["status"] == "online"),
        "stale": sum(1 for a in agents if a["status"] == "stale"),
        "offline": sum(1 for a in agents if a["status"] == "offline"),
        "drifted": sum(1 for a in agents if a["drift_status"] == "drifted"),
    }
    return {"generated_at": now, "summary": summary, "agents": agents, "release_manifest": manifest}
