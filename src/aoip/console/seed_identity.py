"""Seed portal identity THẬT (không fixture runtime, không hardcoded admin).

Tạo user/role/membership cho hai portal — khớp tài khoản OIDC (Dex/Keycloak) theo
EMAIL. Internal subject = email (chuẩn, provider-neutral).

    OMNI_ADMIN_PG_DSN set  → ghi PG (source-of-truth) + mirror Redis.
    không set              → ghi Redis (lab mode).

Chạy:  python -m aoip.console.seed_identity
Env override: AOIP_SEED_TENANT (default "acme"), AOIP_SEED_* emails (xem dưới).

Đây là seed vận-hành (bootstrap operator đầu tiên), KHÔNG phải fixture test.
Mọi tài khoản sau nên tạo qua admin flow. Danh sách ở đây là bootstrap tối thiểu.
"""
from __future__ import annotations

import asyncio
import os

from aoip.console import identity, identity_store

TENANT = os.environ.get("AOIP_SEED_TENANT", "acme")

# (email, provider_role) — provider operators.
PROVIDERS = [
    (os.environ.get("AOIP_SEED_PROVIDER_OWNER", "owner@aoip.dev"), "platform_owner"),
    (os.environ.get("AOIP_SEED_PROVIDER_SUPPORT", "support@aoip.dev"), "support_engineer"),
]
# (email, tenant, tenant_role) — tenant members.
TENANTS = [
    (os.environ.get("AOIP_SEED_TENANT_OWNER", "sre@acme.dev"), TENANT, "tenant_owner"),
    (os.environ.get("AOIP_SEED_TENANT_APPROVER", "approver@acme.dev"), TENANT, "approver"),
    (os.environ.get("AOIP_SEED_TENANT_OTHER", "sre@globex.dev"), "globex", "sre_lead"),
]


async def _seed_redis(redis) -> None:
    for email, role in PROVIDERS:
        await identity.upsert_user(redis, subject=email, email=email)
        await identity.grant_provider_role(redis, subject=email, role=role)
    for email, tenant, role in TENANTS:
        await identity.upsert_user(redis, subject=email, email=email)
        await identity.add_membership(redis, subject=email, tenant=tenant, role=role)


async def _seed_pg(pool, redis) -> None:
    for email, role in PROVIDERS:
        await identity_store.upsert_user(pool, redis, subject=email, email=email)
        await identity_store.grant_provider_role(pool, redis, subject=email, role=role,
                                                 actor="seed")
    for email, tenant, role in TENANTS:
        await identity_store.upsert_user(pool, redis, subject=email, email=email)
        await identity_store.add_membership(pool, redis, subject=email, tenant=tenant,
                                            role=role, actor="seed")


async def main() -> None:
    import redis.asyncio as aioredis
    redis = aioredis.from_url(os.environ.get("OMNI_REDIS_URL", "redis://localhost:6379/0"),
                              decode_responses=True)
    dsn = (os.environ.get("OMNI_ADMIN_PG_DSN") or "").strip()
    if dsn:
        import asyncpg
        from pathlib import Path
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        mig = Path(__file__).resolve().parents[3] / "migrations" / "omni_admin"
        for f in sorted(mig.glob("*.sql")):
            async with pool.acquire() as c:
                async with c.transaction():
                    await c.execute(f.read_text(encoding="utf-8"))
        await _seed_pg(pool, redis)
        await pool.close()
        print(f"seeded PG+Redis: {len(PROVIDERS)} provider, {len(TENANTS)} tenant users")
    else:
        await _seed_redis(redis)
        print(f"seeded Redis (lab): {len(PROVIDERS)} provider, {len(TENANTS)} tenant users")
    for email, role in PROVIDERS:
        print(f"  provider  {email:24s} {role}")
    for email, tenant, role in TENANTS:
        print(f"  tenant    {email:24s} {tenant}/{role}")


if __name__ == "__main__":
    asyncio.run(main())
