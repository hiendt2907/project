import asyncio
import logging

import redis.asyncio as redis_async
from redis.exceptions import BusyLoadingError, ConnectionError as RedisConnectionError
from redis.asyncio.sentinel import Sentinel

from workers.settings import WorkerSettings

logger = logging.getLogger(__name__)

# Redis có thể phình AOF/RDB lớn → load dataset vào memory chậm khi restart.
# Trong lúc đó mọi command raise BusyLoadingError. Worker phải CHỜ thay vì crash-loop.
_REDIS_READY_MAX_WAIT_SEC = 180
_REDIS_READY_POLL_SEC = 2.0


async def wait_until_ready(
    client: redis_async.Redis,
    *,
    max_wait_sec: float = _REDIS_READY_MAX_WAIT_SEC,
    poll_sec: float = _REDIS_READY_POLL_SEC,
) -> None:
    """Poll PING cho tới khi Redis nạp xong dataset (BusyLoadingError hết)."""
    deadline = asyncio.get_event_loop().time() + max_wait_sec
    attempt = 0
    while True:
        try:
            await client.ping()
            if attempt:
                logger.info("event=redis_ready_after_loading attempts=%s", attempt)
            return
        except (BusyLoadingError, RedisConnectionError) as e:
            attempt += 1
            if asyncio.get_event_loop().time() >= deadline:
                logger.error("event=redis_wait_ready_timeout attempts=%s err=%s", attempt, e)
                raise
            logger.warning(
                "event=redis_loading_wait attempt=%s err=%s sleep=%.1fs",
                attempt,
                type(e).__name__,
                poll_sec,
            )
            await asyncio.sleep(poll_sec)


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
    await wait_until_ready(client)
    return client
