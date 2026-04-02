import logging
from typing import Union

import redis.asyncio as redis_async
from redis.asyncio.cluster import RedisCluster
from redis.cluster import ClusterNode

from workers.settings import WorkerSettings

logger = logging.getLogger(__name__)


def create_redis_client(settings: WorkerSettings) -> Union[redis_async.Redis, RedisCluster]:  # type: ignore
    """
    Tạo Redis Client:
    - Nếu OMNI_REDIS_CLUSTER=true: Dùng RedisCluster kết nối thẳng vào cụm.
    - Nếu False (local/test/fallback): Dùng redis_async.Redis.from_url.
    """
    if settings.redis_cluster:
        nodes = [(host.split(":")[0], int(host.split(":")[1]) if ":" in host else 6379) 
                 for host in settings.redis_cluster_nodes.split(",") if host.strip()]
        if not nodes:
            logger.warning("REDIS_CLUSTER=true nhung khong co OMNI_REDIS_CLUSTER_NODES! Fallback...")
        else:
            logger.info("Khoi tao RedisCluster client (nodes=%s)", len(nodes))
            # Build cluster nodes explicitly
            startup_nodes = [ClusterNode(host, port) for host, port in nodes]
            # dynamic_startup_nodes=False: tránh thay thế startup set liên tục (giảm leak ClusterNode
            # khi CLUSTER SLOTS đổi + bug fire-and-forget disconnect trong redis-py).
            # Cụm cố định 6 pod FQDN — không cần động thêm node từ topology.
            return RedisCluster(
                startup_nodes=startup_nodes,
                decode_responses=True,
                dynamic_startup_nodes=False,
                require_full_coverage=False,
            )
            
    logger.info("Khoi tao Redis standalone client qua OMNI_REDIS_URL")
    return redis_async.Redis.from_url(settings.redis_url, decode_responses=True)


async def connect_redis(settings: WorkerSettings) -> Union[redis_async.Redis, RedisCluster]:  # type: ignore
    """
    redis-py 5 asyncio: phải await initialize() trước khi dùng client.
    Bỏ qua bước này gây cảnh báo / leak 'Unclosed ClusterNode object' (asyncio).
    """
    client = create_redis_client(settings)
    await client.initialize()
    return client
