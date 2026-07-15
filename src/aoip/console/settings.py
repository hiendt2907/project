"""Provider Settings — agent enrollment token issuance + credential management.

Product read/write model for the Provider Portal `/settings` route. Reuses
AdminConfigRepo (same PG store IT-3 wrote to) directly — the console BFF talks
to PG/Redis the same way build_provider_overview/build_provider_agents do; it
does not proxy through the gateway's admin API.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any


async def build_provider_settings(pool: Any) -> dict[str, Any]:
    from services.admin_config.repo import AdminConfigRepo

    repo = AdminConfigRepo(pool)
    tenants = await repo.list_tenants()
    credentials_by_tenant = {}
    environments_by_tenant = {}
    for t in tenants:
        credentials_by_tenant[t["tenant_id"]] = await repo.list_agent_credentials(t["tenant_id"])
        environments_by_tenant[t["tenant_id"]] = await repo.list_environments(t["tenant_id"])
    return {"tenants": tenants, "agent_credentials": credentials_by_tenant,
            "environments": environments_by_tenant}


async def issue_enroll_token(
    pool: Any, *, tenant_id: str, actor: str, label: str | None,
    ttl_seconds: int | None, environment_id: str | None = None,
) -> dict[str, Any]:
    """Issue a one-time enroll token. Plaintext is returned exactly once — PG
    only ever stores the sha256 hash (see AdminConfigRepo.create_enroll_token)."""
    from services.admin_config.repo import AdminConfigRepo

    repo = AdminConfigRepo(pool)
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds) if ttl_seconds else None
    )
    result = await repo.create_enroll_token(
        tenant_id=tenant_id, token_hash=token_hash, token_prefix=raw_token[:8],
        actor=actor, label=label, expires_at=expires_at,
        environment_id=environment_id,
    )
    return {"enroll_token": raw_token, **result}


async def revoke_agent_credential(
    pool: Any, redis: Any, *, tenant_id: str, agent_id: str, actor: str,
) -> int:
    """Revoke every active credential of (tenant, agent) + drop the Redis
    auth-cache so the revoke takes effect immediately (IT-3 contract)."""
    from services.admin_config.repo import AdminConfigRepo

    repo = AdminConfigRepo(pool)
    revoked_hashes = await repo.revoke_agent_credentials(
        tenant_id=tenant_id, agent_id=agent_id, actor=actor,
    )
    if redis is not None and revoked_hashes:
        await redis.delete(*[f"omni:agentcred:cache:{h}" for h in revoked_hashes])
    return len(revoked_hashes)
