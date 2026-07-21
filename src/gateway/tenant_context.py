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
    # Set only when auth resolved via a per-agent credential (IT-3 enrollment,
    # omni_admin.agent_credential). That credential is bound to exactly one
    # agent_id in PG at enrollment time — this field carries that binding
    # forward so require_agent_tenant() can enforce it. None for tenant-shared
    # keys, admin keys, and lab/no-auth mode (no per-agent scoping to enforce).
    agent_id: str | None = None


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


async def require_agent_tenant(
    redis: Any, agent_id: str, ctx: TenantContext | None, repo: Any = None,
) -> None:
    """Raise 403 when a non-admin caller targets an agent_id owned by another tenant.

    No-op for admin/no-auth callers.

    Per-agent credential scoping (ctx.agent_id set): a credential enrolled for
    one agent_id must not be usable to register/poll/push-evidence as a
    DIFFERENT agent_id under the same tenant. Without this check, any host
    holding tenant T's per-agent credential could impersonate — or squat the
    identity of — any other agent_id under tenant T, defeating the point of
    per-agent (vs. tenant-shared) credentials.

    Ownership check has two layers:
    1. Ephemeral Redis registry (omni:remote_agent:registry:{agent_id},
       TTL=120s) — fast path, checked first when a live record exists.
    2. Durable PG first-claim record (omni_admin.agent_identity_claim, Phase
       3 of the 0-6 roadmap) — consulted only when the registry has no live
       record (first-ever registration, OR a TTL expiry that a tenant-
       shared-key deployment has no other durable ownership record for).
       Without this second layer, any tenant's key could re-claim an
       agent_id string the moment its owner's registry entry expired —
       confirmed live 2026-07-21 that 2 of 3 real fleet hosts still use
       tenant-shared keys, so this was not a hypothetical gap. `repo=None`
       (lightweight ASGI/unit harnesses without the control-plane repo)
       skips this layer — same as every other repo-gated check in this file.
    """
    if is_admin_ctx(ctx):
        return
    if ctx.agent_id is not None and ctx.agent_id != agent_id:
        raise HTTPException(
            status_code=403,
            detail="credential is scoped to a different agent_id",
        )
    raw = await redis.get(f"{_AGENT_REGISTRY_PREFIX}{agent_id}")
    if raw:
        try:
            owner_tenant_id = json.loads(raw).get("tenant_id")
        except Exception:
            return
        if owner_tenant_id and owner_tenant_id != ctx.tenant_id:
            raise HTTPException(status_code=403, detail="agent_id is registered to a different tenant")
        return
    if repo is not None:
        try:
            durable_owner = await repo.get_or_claim_agent_owner(agent_id, ctx.tenant_id)
        except Exception:
            return  # fail-open on repo error, matching existing registry-lookup exception handling
        if durable_owner != ctx.tenant_id:
            raise HTTPException(status_code=403, detail="agent_id is registered to a different tenant")
