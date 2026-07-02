"""Canonical fresh-tenant provisioning caller (Phase 4-5, "Repeatable Tenant Onboarding Baseline").

Calls ``AdminConfigRepo.create_tenant(idempotent=True)`` directly against the
real ``omni_admin`` Postgres pool — this is the repeat-safe path the HTTP
``POST /autonomy/tenants`` endpoint intentionally does NOT use (it keeps the
409-on-duplicate contract for interactive admin UI callers). Provisioning
tooling that must be safe to re-run (replay, CI, drift-recovery) should call
this instead of the HTTP endpoint.

Usage:
    OMNI_ADMIN_PG_DSN=postgresql://... python scripts/provision_fresh_tenant.py \
        --tenant-id tenant-replay-01 --display-name "Tenant Replay 01"
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os

import asyncpg

from services.admin_config.repo import AdminConfigRepo

logger = logging.getLogger("provision_fresh_tenant")


async def provision(tenant_id: str, display_name: str, actor: str, dsn: str) -> dict:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        repo = AdminConfigRepo(pool)
        result = await repo.create_tenant(
            tenant_id=tenant_id, display_name=display_name, actor=actor, idempotent=True,
        )
        return result
    finally:
        await pool.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--actor", default="provisioning-tooling")
    args = parser.parse_args()

    dsn = (os.environ.get("OMNI_ADMIN_PG_DSN") or "").strip()
    if not dsn:
        raise SystemExit("OMNI_ADMIN_PG_DSN must be set — this script requires a real Postgres pool")

    result = asyncio.run(provision(args.tenant_id, args.display_name, args.actor, dsn))
    logger.info("provisioned tenant=%s result=%s", args.tenant_id, result)


if __name__ == "__main__":
    main()
