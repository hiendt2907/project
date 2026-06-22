"""One-time migration: copy legacy ``omni:rag:sop`` hash → ``omni:rag:sop:default``.

Step-1 of the onboarding-ops-agent plan (agent/plans/PLAN_onboarding_ops_agent.md)
introduces per-tenant key namespacing for the RAG/SOP ledger. The legacy key has
no readers in the live pipeline (audit-only ledger, see rebuild_rag_from_postmortems.py
docstring) but is kept untouched here — copy, never delete — so the new
``{tenant_id}``-scoped convention can roll out without any data-loss risk.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/migrate_rag_sop_tenant.py \
        --redis-url redis://localhost:16379/0
    # dry-run (no write):
    ... --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rag.redis_vector_store import DEFAULT_TENANT_ID, validate_tenant_id  # noqa: E402

logger = logging.getLogger("migrate_rag_sop_tenant")

LEGACY_KEY = "omni:rag:sop"
NEW_KEY_FMT = "omni:rag:sop:{tenant_id}"


async def run_migration(
    *,
    redis_url: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    import redis.asyncio as aioredis

    new_key = NEW_KEY_FMT.format(tenant_id=validate_tenant_id(tenant_id))
    r = aioredis.from_url(redis_url, decode_responses=True)
    try:
        await r.ping()
    except Exception as e:
        logger.error("redis connect failed: %s", e)
        return 1

    try:
        legacy_hlen = await r.hlen(LEGACY_KEY)
        if legacy_hlen == 0:
            logger.info("legacy key %s empty or absent — nothing to migrate", LEGACY_KEY)
            return 0

        existing_hlen = await r.hlen(new_key)
        if existing_hlen > 0 and not force and not dry_run:
            logger.error(
                "event=rag_sop_migration_target_not_empty new_key=%s existing_hlen=%d "
                "— refusing to overwrite; pass --force to proceed anyway",
                new_key, existing_hlen,
            )
            return 1

        entries = await r.hgetall(LEGACY_KEY)
        if dry_run:
            logger.info(
                "dry_run: would copy %d entries %s -> %s", len(entries), LEGACY_KEY, new_key
            )
            return 0

        pipe = r.pipeline(transaction=False)
        for field, value in entries.items():
            pipe.hset(new_key, field, value)
        await pipe.execute()
        new_hlen = await r.hlen(new_key)
        logger.info(
            "migrate complete: legacy_hlen=%d new_hlen=%d legacy_key=%s new_key=%s (legacy key kept, not deleted)",
            legacy_hlen, new_hlen, LEGACY_KEY, new_key,
        )
        if new_hlen != legacy_hlen:
            logger.warning(
                "event=rag_sop_migration_count_mismatch legacy=%d new=%d", legacy_hlen, new_hlen
            )
            return 1
        return 0
    finally:
        await r.aclose()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Migrate omni:rag:sop -> omni:rag:sop:{tenant_id} (copy, no delete)")
    p.add_argument("--redis-url", default=os.environ.get("OMNI_REDIS_URL", "redis://localhost:16379/0"))
    p.add_argument("--tenant-id", default=DEFAULT_TENANT_ID, help="Target tenant for the migrated copy (default: 'default')")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="Overwrite target key even if it already has entries")
    return p.parse_args(argv)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(None)
    rc = asyncio.run(
        run_migration(
            redis_url=args.redis_url,
            tenant_id=args.tenant_id,
            dry_run=args.dry_run,
            force=args.force,
        )
    )
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
