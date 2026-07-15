"""Tenant-scoped auth identity injected into request.state by _require_api_key."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

_AGENT_REGISTRY_PREFIX = "omni:remote_agent:registry:"


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    is_admin: bool
    environment_id: str | None = None


def get_tenant_ctx(request: Any) -> TenantContext | None:
    """Return TenantContext from request.state, or None when auth was not run (lab/tests)."""
    return getattr(getattr(request, "state", None), "tenant", None)


def is_admin_ctx(ctx: TenantContext | None) -> bool:
    """True when ctx is None (no-auth mode) or ctx.is_admin is True."""
    return ctx is None or ctx.is_admin


def resolve_scope(ctx: TenantContext | None, override_tid: str | None = None) -> str | None:
    """Return effective tenant_id to filter by (None = all tenants / global).

    - Lab (ctx=None): None — backward compat, see all
    - Admin: override_tid if provided, else None (aggregate all)
    - Non-admin: ctx.tenant_id — override_tid is ignored
    """
    if ctx is None:
        return None
    if ctx.is_admin:
        return override_tid
    return ctx.tenant_id


async def require_agent_tenant(redis: Any, agent_id: str, ctx: TenantContext | None) -> None:
    """Raise 403 when a non-admin caller targets an agent_id owned by another tenant.

    No-op for admin/no-auth callers, and for agent_ids with no existing registry
    record yet (first registration is always allowed — ownership is established
    by whoever registers first).
    """
    if is_admin_ctx(ctx):
        return
    raw = await redis.get(f"{_AGENT_REGISTRY_PREFIX}{agent_id}")
    if not raw:
        return
    try:
        owner_tenant_id = json.loads(raw).get("tenant_id")
    except Exception:
        return
    if owner_tenant_id and owner_tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=403, detail="agent_id is registered to a different tenant")
