#!/usr/bin/env python3
"""One-shot migration: copy old KPI ZSET keys to per-tenant keys.

Old: omni:kpi:z:accepted|rejected|false_positive
New: omni:kpi:z:default:accepted|rejected|false_positive

Run once after deploying kpi_metrics.py per-tenant changes.
Safe to run multiple times (ZUNIONSTORE with destination = new key, preserves existing).
"""
from __future__ import annotations

import asyncio
import os
import sys

import redis.asyncio as aioredis


_OLD_NEW = [
    ("omni:kpi:z:accepted",      "omni:kpi:z:default:accepted"),
    ("omni:kpi:z:rejected",      "omni:kpi:z:default:rejected"),
    ("omni:kpi:z:false_positive", "omni:kpi:z:default:false_positive"),
]

_DETECTED_LANES = ["SYS_RESOURCE", "SYS_HARD_FAIL", "APP_HTTP", "SIEM_SECURITY"]
_OLD_LANE_KEYS = [
    (f"omni:kpi:detected:{lane}", f"omni:kpi:detected:default:{lane}")
    for lane in _DETECTED_LANES
] + [
    (f"omni:kpi:resolved:{lane}", f"omni:kpi:resolved:default:{lane}")
    for lane in _DETECTED_LANES
]


async def migrate(redis_url: str) -> None:
    r = aioredis.from_url(redis_url, decode_responses=True)
    try:
        all_pairs = _OLD_NEW + _OLD_LANE_KEYS
        for old_key, new_key in all_pairs:
            exists = await r.exists(old_key)
            if not exists:
                print(f"  SKIP  {old_key} (does not exist)")
                continue
            count = await r.zcard(old_key)
            # ZUNIONSTORE merges into new_key (safe if new_key already has entries)
            merged = await r.zunionstore(new_key, [new_key, old_key])
            # Preserve TTL from old key if new key has no expiry
            old_ttl = await r.ttl(old_key)
            new_ttl = await r.ttl(new_key)
            if new_ttl == -1 and old_ttl > 0:
                await r.expire(new_key, old_ttl)
            print(f"  OK    {old_key} → {new_key}  members={count} merged_total={merged}")
        print("Migration complete.")
    finally:
        await r.aclose()


if __name__ == "__main__":
    url = os.getenv("OMNI_REDIS_URL", "redis://localhost:16379/0")
    if len(sys.argv) > 1:
        url = sys.argv[1]
    print(f"Migrating KPI keys on {url} ...")
    asyncio.run(migrate(url))
