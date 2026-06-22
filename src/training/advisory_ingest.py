"""Ingest advisory JSONL samples into Redis omni:rag:sop hash + optional HNSW vector store."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import redis.asyncio as aioredis

from rag.redis_vector_store import DEFAULT_TENANT_ID, validate_tenant_id

logger = logging.getLogger(__name__)

REDIS_SOB_KEY_FMT = "omni:rag:sop:{tenant_id}"


def _sop_key(tenant_id: str) -> str:
    return REDIS_SOB_KEY_FMT.format(tenant_id=validate_tenant_id(tenant_id))


async def run_advisory_ingest(
    *,
    jsonl_path: Path,
    redis_url: str,
    dry_run: bool = False,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> int:
    """Ingest advisory JSONL → Redis hash omni:rag:sop:{tenant_id}."""
    sop_key = _sop_key(tenant_id)
    r = aioredis.from_url(redis_url, decode_responses=True)
    try:
        await r.ping()
    except Exception as e:
        logger.error("redis connect failed: %s", e)
        return 1

    lines = jsonl_path.read_text().strip().splitlines()
    valid = 0
    pipe = r.pipeline(transaction=False)

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception as e:
            logger.warning("skip invalid json: %s", e)
            continue

        alert_id = str(obj.get("alert_id") or "")
        if not alert_id:
            continue

        if not dry_run:
            pipe.hset(sop_key, alert_id, json.dumps(obj, ensure_ascii=False))
        valid += 1

    if not dry_run:
        await pipe.execute()
        count = await r.hlen(sop_key)
        logger.info(
            "advisory_ingest complete: tenant_id=%s ingested=%d redis_hlen=%d",
            tenant_id, valid, count,
        )
    else:
        logger.info("advisory_ingest dry_run: tenant_id=%s would ingest %d entries", tenant_id, valid)

    await r.aclose()
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest advisory JSONL → Redis omni:rag:sop:{tenant_id}")
    p.add_argument("--path", required=True, help="Path to JSONL file")
    p.add_argument("--redis-url", default=os.environ.get("OMNI_REDIS_URL", "redis://localhost:16379/0"))
    p.add_argument("--tenant-id", default=DEFAULT_TENANT_ID, help="Tenant isolation key (default: 'default')")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", action="store_true", dest="verbose")
    return p.parse_args(argv)


async def _amain(argv: list[str] | None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    path = Path(args.path)
    if not path.is_file():
        logger.error("file not found: %s", path)
        return 2
    return await run_advisory_ingest(
        jsonl_path=path,
        redis_url=args.redis_url,
        dry_run=args.dry_run,
        tenant_id=args.tenant_id,
    )


def main() -> None:
    raise SystemExit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
