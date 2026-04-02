import logging

import redis.asyncio as redis_async

from workers.settings import WorkerSettings

logger = logging.getLogger(__name__)


def create_redis_client(settings: WorkerSettings) -> redis_async.Redis:
    """Redis standalone client via ``OMNI_REDIS_URL`` (Streams / locks)."""
    logger.info("Khoi tao Redis standalone client qua OMNI_REDIS_URL")
    return redis_async.Redis.from_url(settings.redis_url, decode_responses=True)


async def connect_redis(settings: WorkerSettings) -> redis_async.Redis:
    """redis-py 5 asyncio: await ``initialize()`` before use."""
    client = create_redis_client(settings)
    await client.initialize()
    return client
