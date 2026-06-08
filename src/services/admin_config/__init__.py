"""Admin config store — PostgreSQL source-of-truth cho Admin UI (MASTER_PLAN §6.5).

Public surface:
- ``create_admin_pool`` / ``run_migrations`` — kết nối + migrate schema omni_admin.
- ``AdminConfigRepo`` — async repo (write-through cache + Transactional Outbox).
- ``CratOutboxDrainer`` — background loop ghi outbox → CRAT block (at-least-once).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from services.admin_config.cache import (
    cache_key_runtime_flag,
    cache_key_tier,
    invalidate_cache,
    read_tier_cached,
    write_through_cache,
)
from services.admin_config.pool import create_admin_pool, run_migrations
from services.admin_config.repo import AdminConfigRepo, OptimisticLockError

if TYPE_CHECKING:
    from services.admin_config.drainer import CratOutboxDrainer

__all__ = [
    "AdminConfigRepo",
    "CratOutboxDrainer",
    "OptimisticLockError",
    "cache_key_runtime_flag",
    "cache_key_tier",
    "create_admin_pool",
    "invalidate_cache",
    "read_tier_cached",
    "run_migrations",
    "write_through_cache",
]


def __getattr__(name: str) -> Any:
    # Lazy: drainer kéo theo audit_ledger.chain_writer (worker-side). Gateway chỉ cần
    # repo/pool/cache → tránh import chain_writer ở load-time của package.
    if name == "CratOutboxDrainer":
        from services.admin_config.drainer import CratOutboxDrainer

        return CratOutboxDrainer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
