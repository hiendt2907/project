"""Resolve risk-class hiệu lực theo tenant: override (DB→cache) phủ lên bảng tĩnh.

Hot-path đọc Redis cache trước (`omni:cfg:risk:{tenant}:{tool}`), miss → Postgres
override → set cache (kể cả negative bằng sentinel để khỏi query lại). Repo None /
DB vắng → fallback bảng tĩnh thuần (fail-closed HIGH cho tool lạ).
"""

from __future__ import annotations

import logging
from typing import Any

from services.admin_config.cache import cache_key_risk_class
from workers.risk_class import VALID_RISK_CLASSES, risk_class_of

logger = logging.getLogger(__name__)

# Sentinel cache cho "không có override" — tránh query Postgres lặp lại.
_NO_OVERRIDE = "__none__"
_CACHE_TTL = 300


class RiskClassResolver:
    """Phủ override lên ``risk_class_of``. ``repo``/``redis`` optional."""

    def __init__(self, *, repo: Any = None, redis: Any = None) -> None:
        self._repo = repo
        self._redis = redis

    async def resolve(self, tool_name: str, *, tenant_id: str = "default") -> str:
        override = await self._read_override(tool_name, tenant_id)
        return risk_class_of(tool_name, override=override)

    async def _read_override(self, tool_name: str, tenant_id: str) -> str | None:
        key = cache_key_risk_class(tenant_id, tool_name)
        # 1. cache
        if self._redis is not None:
            try:
                cached = await self._redis.get(key)
            except Exception as exc:  # noqa: BLE001 — cache best-effort
                logger.warning("risk_class cache read fail tool=%s err=%s", tool_name, exc)
                cached = None
            if cached == _NO_OVERRIDE:
                return None
            if cached in VALID_RISK_CLASSES:
                return cached
        # 2. Postgres
        if self._repo is None:
            return None
        override = await self._repo.get_risk_class_override(tool_name, tenant_id)
        # 3. nạp cache (kể cả negative)
        if self._redis is not None:
            try:
                await self._redis.set(key, override or _NO_OVERRIDE, ex=_CACHE_TTL)
            except Exception as exc:  # noqa: BLE001
                logger.warning("risk_class cache set fail tool=%s err=%s", tool_name, exc)
        return override
