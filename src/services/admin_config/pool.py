"""asyncpg pool + migration runner cho schema ``omni_admin``.

Pool rỗng (DSN không set) → trả ``None``; caller phải fail-closed về env default
(không tự ghi config khi DB vắng). Migration idempotent (CREATE ... IF NOT EXISTS).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Thư mục migration mặc định (raw SQL, chạy theo thứ tự tên file).
_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations" / "omni_admin"


async def create_admin_pool(settings: Any) -> Any | None:
    """Tạo asyncpg pool từ ``settings.admin_pg_dsn``. Rỗng → None (store disabled)."""
    dsn = (getattr(settings, "admin_pg_dsn", "") or "").strip()
    if not dsn:
        logger.info("admin_config: OMNI_ADMIN_PG_DSN rỗng — Admin config store disabled")
        return None
    import asyncpg  # local import: chỉ cần khi DSN set

    pool = await asyncpg.create_pool(
        dsn,
        min_size=int(getattr(settings, "admin_pg_pool_min", 1)),
        max_size=int(getattr(settings, "admin_pg_pool_max", 8)),
    )
    logger.info("admin_config: asyncpg pool ready (omni_admin)")
    return pool


async def run_migrations(pool: Any, *, migrations_dir: Path | None = None) -> list[str]:
    """Chạy lần lượt mọi ``*.sql`` (sorted) trong thư mục migration. Idempotent.

    Trả danh sách tên file đã apply. Mỗi file chạy trong 1 transaction.
    """
    directory = migrations_dir or _MIGRATIONS_DIR
    files = sorted(p for p in directory.glob("*.sql"))
    applied: list[str] = []
    for sql_file in files:
        sql = sql_file.read_text(encoding="utf-8")
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(sql)
        applied.append(sql_file.name)
        logger.info("admin_config: migration applied %s", sql_file.name)
    return applied
