"""Durable Redis read-model for Mission runtime objects.

The Mission remains an AOIP domain object; this module is only its operational
projection.  It stores no evidence, credentials, or customer document content.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from urllib.parse import quote, unquote

from aoip.mission import Mission, MissionState

_PREFIX = "omni:mission:"
_INDEX = "omni:mission:index:"


def _part(value: str) -> str:
    return quote(str(value), safe="")


def _key(tenant_id: str, mission_id: str) -> str:
    return f"{_PREFIX}{_part(tenant_id)}:{_part(mission_id)}"


def _index(tenant_id: str) -> str:
    return f"{_INDEX}{_part(tenant_id)}"


def _decode(raw: str | bytes | None) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


class MissionStore:
    """Tenant-scoped Mission persistence and list projection."""

    def __init__(self, redis, *, ttl_seconds: int | None = None):
        self.redis = redis
        self.ttl_seconds = ttl_seconds

    async def save(self, tenant_id: str, mission: Mission, *, updated_at: float | None = None,
                   next_action: str | None = None, last_activity: str | None = None) -> dict:
        now = float(updated_at if updated_at is not None else time.time())
        payload = asdict(mission)
        payload["state"] = mission.state.value
        payload.update({"tenant_id": str(tenant_id), "updated_at": now})
        if next_action is not None:
            payload["next_action"] = next_action
        if last_activity is not None:
            payload["last_activity"] = last_activity
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        key = _key(tenant_id, mission.mission_id)
        await self.redis.set(key, encoded)
        await self.redis.zadd(_index(tenant_id), {mission.mission_id: now})
        if self.ttl_seconds:
            await self.redis.expire(key, self.ttl_seconds)
        return payload

    async def get(self, tenant_id: str, mission_id: str) -> dict | None:
        payload = _decode(await self.redis.get(_key(tenant_id, mission_id)))
        if payload is None or payload.get("tenant_id") != str(tenant_id):
            return None
        return payload

    async def list(self, tenant_id: str, *, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        ids = await self.redis.zrevrange(_index(tenant_id), 0, limit - 1)
        out = []
        for mission_id in ids:
            item = await self.get(tenant_id, unquote(str(mission_id)))
            if item is not None:
                out.append(item)
        return out

    async def list_all(self, *, limit: int = 500) -> list[dict]:
        """Provider projection; tenant IDs are retained for operator context."""
        keys = await self.redis.keys(f"{_INDEX}*")
        out: list[dict] = []
        for index_key in sorted(keys):
            encoded_tenant = str(index_key).split(_INDEX, 1)[-1]
            tenant_id = unquote(encoded_tenant)
            out.extend(await self.list(tenant_id, limit=limit))
        out.sort(key=lambda item: float(item.get("updated_at", 0)), reverse=True)
        return out[: max(1, min(int(limit), 500))]


def mission_from_payload(payload: dict) -> Mission:
    """Decode only the canonical Mission fields from a projection."""
    return Mission(
        mission_id=str(payload["mission_id"]), goal=str(payload["goal"]),
        scope=str(payload["scope"]), state=MissionState(str(payload["state"])),
        completion=float(payload.get("completion", 0.0)),
        dod_passed=tuple(payload.get("dod_passed") or ()),
        dod_failed=tuple(payload.get("dod_failed") or ()),
        parent_mission_id=payload.get("parent_mission_id"),
    )
