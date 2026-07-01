"""ASGI entrypoints production cho hai portal — PG source-of-truth + Redis hot-path.

Chạy (compose/systemd):
    uvicorn aoip.console.server:provider_app --host 0.0.0.0 --port 8081
    uvicorn aoip.console.server:tenant_app   --host 0.0.0.0 --port 8082

Lifespan: connect Redis + asyncpg pool, chạy migration omni_admin, hydrate portal
identity PG→Redis. Không có OMNI_ADMIN_PG_DSN ⇒ lab mode (Redis-only provisioning).
OIDC config lấy từ env AOIP_OIDC_{PROVIDER,TENANT}_* (provider-neutral, Dex/Keycloak).
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from aoip.console import identity_store
from aoip.console.app import create_provider_app, create_tenant_app

logger = logging.getLogger(__name__)


def _redis():
    import redis.asyncio as aioredis
    url = os.environ.get("OMNI_REDIS_URL", "redis://localhost:6379/0")
    return aioredis.from_url(url, decode_responses=True)


async def _pg_pool():
    dsn = (os.environ.get("OMNI_ADMIN_PG_DSN") or "").strip()
    if not dsn:
        logger.info("portal: OMNI_ADMIN_PG_DSN rỗng — lab mode (Redis-only provisioning)")
        return None
    import asyncpg
    from pathlib import Path
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
    # Migration idempotent: chạy toàn bộ omni_admin/*.sql theo thứ tự.
    mig_dir = Path(__file__).resolve().parents[3] / "migrations" / "omni_admin"
    for sql_file in sorted(mig_dir.glob("*.sql")):
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(sql_file.read_text(encoding="utf-8"))
    logger.info("portal: omni_admin migrations applied")
    return pool


def _build(kind: str):
    """Factory chung: tạo app + gắn lifespan hydrate PG→Redis."""
    redis = _redis()

    @asynccontextmanager
    async def lifespan(app):
        pool = await _pg_pool()
        app.state.pool = pool
        app.state.redis = redis
        if pool is not None:
            counts = await identity_store.hydrate_from_pg(pool, redis)
            logger.info("portal[%s]: hydrated %s from PG", kind, counts)
        yield
        if pool is not None:
            await pool.close()

    if kind == "provider":
        app = create_provider_app(redis)
    else:
        app = create_tenant_app(redis)
    app.router.lifespan_context = lifespan
    return app


provider_app = _build("provider")
tenant_app = _build("tenant")
