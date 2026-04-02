"""Ghi ledger lỗi lên Postgres HA (`itops_error_ledger`) — an toàn qua pgpool."""

from __future__ import annotations

import logging
import re
import traceback
from typing import Any

# Tránh lưu token Telegram trong traceback (URL getUpdates).
_RE_TELEGRAM_BOT_URL = re.compile(r"https://api\.telegram\.org/bot[^/\s]+")


def _sanitize_ledger_text(text: str) -> str:
    if not text:
        return text
    return _RE_TELEGRAM_BOT_URL.sub("https://api.telegram.org/bot[REDACTED]", text)


import asyncpg

from rag.pgvector_store import (
    COLLECTION_ERRORS,
    PostgresRAGSettings,
    init_pg_pool,
    log_error_to_ledger,
)

logger = logging.getLogger(__name__)


class ErrorLedger:
    """Ghi lỗi có swallow (không làm sập worker), tự retry qua Tenacity trong pgvector_store."""

    def __init__(self, pool: asyncpg.Pool, *, owns_pool: bool = False) -> None:
        self._pool = pool
        self._owns_pool = owns_pool

    @classmethod
    async def from_settings(cls, settings: PostgresRAGSettings | None = None) -> ErrorLedger:
        s = settings or PostgresRAGSettings()
        pool = await init_pg_pool(s)
        return cls(pool, owns_pool=True)

    async def aclose(self) -> None:
        if self._owns_pool:
            await self._pool.close()

    async def ensure_ready(self) -> None:
        """Create extension and tables if not exist."""
        from rag.pgvector_store import PGVectorStore
        store = PGVectorStore(self._pool)
        await store.ensure_ready()

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
                self._pool,
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
                    "error_ledger_write_failed_pgHA",
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
