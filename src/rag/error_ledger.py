"""Error ledger — Redis SETEX backend (Postgres removed)."""

from __future__ import annotations

import json
import logging
import re
import traceback
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis

from rag.redis_vector_store import (
    COLLECTION_ERRORS,
    RedisVectorStore,
    log_error_to_ledger,
)

logger = logging.getLogger(__name__)

_RE_TELEGRAM_BOT_URL = re.compile(r"https://api\.telegram\.org/bot[^/\s]+")

_ERROR_TTL_SEC = 7 * 24 * 3600  # 7 days


def _sanitize_ledger_text(text: str) -> str:
    if not text:
        return text
    return _RE_TELEGRAM_BOT_URL.sub("https://api.telegram.org/bot[REDACTED]", text)


class ErrorLedger:
    """Ghi lỗi — Redis SETEX (TTL-based, không cần vector index)."""

    def __init__(self, r: aioredis.Redis, *, _owns_pool: bool = False) -> None:
        self._r = r
        # _vector_store used for log_error_to_ledger (upserts with stable vector)
        self._vector_store = RedisVectorStore(r)

    @classmethod
    async def from_redis(cls, r: aioredis.Redis) -> "ErrorLedger":
        return cls(r)

    async def aclose(self) -> None:
        pass  # shared connection owned by caller

    async def ensure_ready(self) -> None:
        await self._vector_store._ensure_index(COLLECTION_ERRORS)

    async def record_error(
        self,
        *,
        title: str,
        detail: str,
        phase: str,
        component: str = "",
        extra: dict[str, Any] | None = None,
        collection_name: str = COLLECTION_ERRORS,
        swallow_errors: bool = True,
    ) -> str | None:
        try:
            return await log_error_to_ledger(
                self._vector_store,
                title=title,
                detail=_sanitize_ledger_text(detail),
                phase=phase,
                component=component,
                extra=extra,
                collection_name=collection_name,
            )
        except Exception:
            if swallow_errors:
                logger.exception(
                    "error_ledger_write_failed",
                    extra={"phase": phase, "component": component, "title": title},
                )
                return None
            raise

    async def record_exception(
        self,
        exc: BaseException,
        *,
        phase: str,
        component: str,
        title: str = "",
        extra: dict[str, Any] | None = None,
        swallow_errors: bool = True,
    ) -> str | None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        t = title or type(exc).__name__
        return await self.record_error(
            title=t,
            detail=tb,
            phase=phase,
            component=component,
            extra=extra,
            swallow_errors=swallow_errors,
        )
