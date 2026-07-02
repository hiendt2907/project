"""Canonical fresh-tenant provisioning caller (Phase 4-5, "Repeatable Tenant Onboarding Baseline").

Calls ``AdminConfigRepo.create_tenant(idempotent=True)`` directly against the
real ``omni_admin`` Postgres pool — this is the repeat-safe path the HTTP
``POST /autonomy/tenants`` endpoint intentionally does NOT use (it keeps the
409-on-duplicate contract for interactive admin UI callers). Provisioning
tooling that must be safe to re-run (replay, CI, drift-recovery) should call
this instead of the HTTP endpoint.

After the Postgres row is provisioned, this also runs
``scripts/add_tenant_api_key.sh`` to provision the tenant's gateway API key
(idempotent — see iteration 10), so a single command covers both steps that
were previously run by hand in sequence. Pass ``--skip-api-key`` to
provision only the Postgres row.

Usage:
    OMNI_ADMIN_PG_DSN=postgresql://... python scripts/provision_fresh_tenant.py \
        --tenant-id tenant-replay-01 --display-name "Tenant Replay 01"
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
from pathlib import Path

import asyncpg

from services.admin_config.repo import AdminConfigRepo

logger = logging.getLogger("provision_fresh_tenant")

ADD_API_KEY_SCRIPT = Path(__file__).parent / "add_tenant_api_key.sh"


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


def provision_api_key(tenant_id: str) -> None:
    subprocess.run(["bash", str(ADD_API_KEY_SCRIPT), tenant_id], check=True, cwd=Path(__file__).parent.parent)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--actor", default="provisioning-tooling")
    parser.add_argument(
        "--skip-api-key", action="store_true",
        help="Only provision the Postgres tenant row; skip gateway API-key provisioning",
    )
    args = parser.parse_args()

    dsn = (os.environ.get("OMNI_ADMIN_PG_DSN") or "").strip()
    if not dsn:
        raise SystemExit("OMNI_ADMIN_PG_DSN must be set — this script requires a real Postgres pool")

    result = asyncio.run(provision(args.tenant_id, args.display_name, args.actor, dsn))
    logger.info("provisioned tenant=%s result=%s", args.tenant_id, result)

    if not args.skip_api_key:
        logger.info("provisioning gateway API key for tenant=%s", args.tenant_id)
        provision_api_key(args.tenant_id)


if __name__ == "__main__":
    main()
