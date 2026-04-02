"""Clear stuck Omni worker Redis state: XACK pending, XTRIM flush streams, delayed queue, locks.

Flushes `events:inbound` and `incidents:proactive` (trim to zero entries after ACK).

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


async def _ack_all_pending(stream: str, group: str, r: Any) -> int:
    n = 0
    while True:
        batch = await r.xpending_range(stream, group, min="-", max="+", count=500)
        if not batch:
            break
        ids: list[str] = []
        for item in batch:
            if isinstance(item, dict):
                mid = item.get("message_id")
            else:
                mid = item[0] if item else None
            if mid is not None:
                ids.append(str(mid))
        if not ids:
            break
        await r.xack(stream, group, *ids)
        n += len(ids)
    return n


async def _scan_delete_pattern(r: Any, pattern: str) -> int:
    n = 0
    async for key in r.scan_iter(match=pattern, count=200):
        await r.delete(key)
        n += 1
    return n


async def _xtrim_all(stream: str, r: Any) -> int:
    """Drop every entry (queue flush). Consumer group remains; worker recreates entries on XADD."""
    try:
        n = await r.execute_command("XTRIM", stream, "MAXLEN", "0")
        return int(n) if n is not None else 0
    except Exception:
        try:
            n = await r.xtrim(stream, maxlen=0, approximate=False)  # type: ignore[call-arg]
            return int(n) if n is not None else 0
        except Exception as e:
            logging.warning("XTRIM %s failed: %s", stream, e)
            return -1


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    s = WorkerSettings()
    r = await connect_redis(s)
    try:
        streams_groups: list[tuple[str, str]] = [
            (s.stream_inbound, s.consumer_group),
            (s.stream_incidents_proactive, s.consumer_group_proactive),
        ]
        for stream, group in streams_groups:
            acked = await _ack_all_pending(stream, group, r)
            logging.info("XACK pending: %d (stream=%s group=%s)", acked, stream, group)
            trimmed = await _xtrim_all(stream, r)
            logging.info("XTRIM flush: stream=%s trimmed=%s", stream, trimmed)

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
