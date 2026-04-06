"""Clear stuck Omni worker Redis state: delayed queue, locks, circuit breaker (Kafka bus is separate).

Kafka topics are not flushed here — use broker tooling (consumer group reset / topic delete) if needed.

Run inside the cluster (worker pod has Redis reachability + settings):

  kubectl exec -n multi-agent deploy/omni-worker -- env PYTHONPATH=/app/src python -m devtools.redis_cleanup_stuck

Or from dev machine if Redis is reachable with same env as WorkerSettings.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from workers.redis_client import connect_redis
from workers.settings import WorkerSettings

logger = logging.getLogger(__name__)


async def _scan_delete_pattern(r: Any, pattern: str) -> int:
    n = 0
    async for key in r.scan_iter(match=pattern, count=200):
        await r.delete(key)
        n += 1
    return n


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    s = WorkerSettings()
    r = await connect_redis(s)
    try:
        dq = await r.delete("omni:delayed_queue")
        logging.info("DEL omni:delayed_queue -> %s", dq)

        locks = await _scan_delete_pattern(r, "omni:lock:*")
        logging.info("Deleted omni:lock:* keys: %d", locks)

        retries = await _scan_delete_pattern(r, "omni:retry:*")
        logging.info("Deleted omni:retry:* keys: %d", retries)

        cb = await r.delete("omni:circuit_breaker:active")
        logging.info("DEL omni:circuit_breaker:active -> %s", cb)
    finally:
        await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
