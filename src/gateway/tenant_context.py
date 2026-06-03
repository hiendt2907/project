"""Tenant-scoped auth identity injected into request.state by _require_api_key."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    is_admin: bool


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
