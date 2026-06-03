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

logger = logging.getLogger(__name__)

REDIS_SOB_KEY = "omni:rag:sop"


async def run_advisory_ingest(
    *,
    jsonl_path: Path,
    redis_url: str,
    dry_run: bool = False,
) -> int:
    """Ingest advisory JSONL → Redis hash omni:rag:sop."""
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
            pipe.hset(REDIS_SOB_KEY, alert_id, json.dumps(obj, ensure_ascii=False))
        valid += 1

    if not dry_run:
        await pipe.execute()
        count = await r.hlen(REDIS_SOB_KEY)
        logger.info("advisory_ingest complete: ingested=%d redis_hlen=%d", valid, count)
    else:
        logger.info("advisory_ingest dry_run: would ingest %d entries", valid)

    await r.aclose()
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest advisory JSONL → Redis omni:rag:sop")
    p.add_argument("--path", required=True, help="Path to JSONL file")
    p.add_argument("--redis-url", default=os.environ.get("OMNI_REDIS_URL", "redis://localhost:16379/0"))
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
    )


def main() -> None:
    raise SystemExit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
