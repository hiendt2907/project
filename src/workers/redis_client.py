import logging

import redis.asyncio as redis_async
from redis.asyncio.sentinel import Sentinel

from workers.settings import WorkerSettings

logger = logging.getLogger(__name__)


def _sentinel_endpoints(hosts_csv: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for part in hosts_csv.split(","):
        p = part.strip()
        if not p:
            continue
        if ":" in p:
            h, port_s = p.rsplit(":", 1)
            out.append((h.strip(), int(port_s.strip())))
        else:
            out.append((p, 26379))
    return out


def create_redis_client(settings: WorkerSettings) -> redis_async.Redis:
    """Redis: ``OMNI_REDIS_URL`` standalone, or Sentinel when ``OMNI_REDIS_SENTINEL_HOSTS`` is set."""
    sent_csv = (settings.redis_sentinel_hosts or "").strip()
    if sent_csv:
        endpoints = _sentinel_endpoints(sent_csv)
        if not endpoints:
            raise ValueError("OMNI_REDIS_SENTINEL_HOSTS is set but parses to no endpoints")
        logger.info(
            "Redis Sentinel master=%s sentinels=%s",
            settings.redis_sentinel_master_name,
            endpoints,
        )
        sentinel = Sentinel(endpoints, socket_timeout=0.5, decode_responses=True)
        return sentinel.master_for(
            settings.redis_sentinel_master_name,
            socket_timeout=0.5,
            decode_responses=True,
        )
    logger.info("Redis standalone client via OMNI_REDIS_URL")
    return redis_async.Redis.from_url(settings.redis_url, decode_responses=True)


async def connect_redis(settings: WorkerSettings) -> redis_async.Redis:
    """redis-py 5 asyncio: await ``initialize()`` before use."""
    client = create_redis_client(settings)
    await client.initialize()
    return client
