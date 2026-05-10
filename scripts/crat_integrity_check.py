#!/usr/bin/env python3
"""Standalone CRAT audit chain integrity checker — runs as K8s CronJob."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import redis.asyncio as aioredis

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

REDIS_CHAIN_KEY = "audit_chain:blocks"
METRIC_FILE = os.getenv("CRAT_METRIC_FILE", "/tmp/crat_integrity.prom")


async def main() -> int:
    redis_url = os.getenv("OMNI_REDIS_URL", "redis://redis:6379/0")
    r = await aioredis.from_url(redis_url, decode_responses=True)
    try:
        raw_blocks = await r.lrange(REDIS_CHAIN_KEY, 0, -1)
    finally:
        await r.aclose()

    if not raw_blocks:
        logger.info("CRAT chain is empty — no blocks to verify")
        _write_metric(1, 0, "empty_chain")
        return 0

    blocks = []
    for raw in raw_blocks:
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError as e:
            logger.error("CRAT block parse error: %r", e)
            _write_metric(0, len(blocks), "parse_error")
            return 1

    # Import verifier from project src (mounted as volume in CronJob)
    sys.path.insert(0, "/app/src")
    from services.audit_ledger.verifier import verify_chain  # type: ignore[import]

    result = verify_chain(blocks)
    if result.ok:
        logger.info("CRAT chain OK — %d blocks verified", result.blocks_checked)
        _write_metric(1, result.blocks_checked, result.reason)
        return 0
    else:
        logger.error(
            "CRAT chain BROKEN at seq=%s reason=%s errors=%s",
            result.first_broken_seq, result.reason, result.errors,
        )
        _write_metric(0, result.blocks_checked, result.reason)
        return 1


def _write_metric(ok: int, blocks_checked: int, reason: str) -> None:
    content = (
        f"# HELP omni_crat_integrity_ok CRAT audit chain integrity (1=ok, 0=broken)\n"
        f"# TYPE omni_crat_integrity_ok gauge\n"
        f'omni_crat_integrity_ok{{reason="{reason}"}} {ok}\n'
        f"# HELP omni_crat_blocks_checked Number of blocks verified in last run\n"
        f"# TYPE omni_crat_blocks_checked gauge\n"
        f"omni_crat_blocks_checked {blocks_checked}\n"
    )
    try:
        with open(METRIC_FILE, "w") as f:
            f.write(content)
        logger.info("Metrics written to %s", METRIC_FILE)
    except OSError as e:
        logger.warning("Could not write metrics file: %r", e)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
