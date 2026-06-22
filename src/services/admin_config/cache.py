"""Write-through Redis cache helpers cho hot-path gate.

Postgres = source-of-truth; Redis = cache đọc nhanh. Ghi cache SAU khi Postgres
commit; cache fail chỉ log (Postgres vẫn đúng). Hot-path gate đọc cache trước,
miss → fallback Postgres → miss → env default.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# TTL cache config (giây). Đủ ngắn để invalidations lỡ vẫn tự lành; đủ dài để
# tránh query Postgres mỗi mutate.
CACHE_TTL_SEC = 300


def cache_key_tier(tenant_id: str) -> str:
    return f"omni:cfg:tier:{tenant_id}"


def cache_key_runtime_flag(tenant_id: str, flag_key: str) -> str:
    return f"omni:cfg:flag:{tenant_id}:{flag_key}"


def cache_key_risk_class(tenant_id: str, tool_name: str) -> str:
    return f"omni:cfg:risk:{tenant_id}:{tool_name}"


def cache_key_readiness(tenant_id: str) -> str:
    return f"omni:cfg:readiness:{tenant_id}"


async def write_through_cache(redis: Any, key: str, value: str, *, ttl: int = CACHE_TTL_SEC) -> None:
    """Set cache key. Redis fail → log + nuốt (không phá luồng; Postgres là sự thật)."""
    if redis is None:
        return
    try:
        await redis.set(key, value, ex=ttl)
    except Exception as exc:  # noqa: BLE001 — cache best-effort
        logger.warning("admin_config: write-through cache fail key=%s err=%s", key, exc)


async def invalidate_cache(redis: Any, key: str) -> None:
    """Xoá cache key (gọi khi config đổi). Best-effort."""
    if redis is None:
        return
    try:
        await redis.delete(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("admin_config: cache invalidate fail key=%s err=%s", key, exc)


async def read_tier_cached(redis: Any, tenant_id: str) -> str | None:
    """Đọc tier từ cache. None = miss (caller fallback Postgres → env default)."""
    if redis is None:
        return None
    try:
        return await redis.get(cache_key_tier(tenant_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("admin_config: cache read fail tenant=%s err=%s", tenant_id, exc)
        return None
