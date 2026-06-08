"""Remote-agent anomaly thresholds, served from omni_admin runtime flags.

The remote agent is a sensor on customer hosts that Prometheus cannot scrape.
Its PASS/FAIL anomaly call needs warn thresholds — historically hard-coded in
``remote_agent/collectors/system.py``, which meant tuning them required
redeploying the agent on every customer host.

This module resolves those thresholds from the write-through Redis cache
(``omni:cfg:flag:{tenant}:agent.{cpu,mem,disk}_warn``), source-of-truth
``omni_admin.runtime_flag``. On any miss/parse error it falls back to safe
defaults, so the agent always gets a usable bundle. The resolved bundle is
returned in the ``/webhook/agent/register`` response and applied by the agent
without a restart.
"""

from __future__ import annotations

import logging
from typing import Any

from services.admin_config.cache import cache_key_runtime_flag

logger = logging.getLogger(__name__)

# Safe defaults — mirror the historical constants in system.py so behaviour is
# unchanged until an operator sets a flag.
DEFAULT_CPU_WARN = 80.0
DEFAULT_MEM_WARN = 85.0
DEFAULT_DISK_WARN = 90.0

_FLAG_CPU = "agent.cpu_warn"
_FLAG_MEM = "agent.mem_warn"
_FLAG_DISK = "agent.disk_warn"


def _coerce_pct(raw: Any, default: float) -> float:
    """Parse a percentage flag value; clamp to (0, 100]; fall back on garbage."""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    if val <= 0.0 or val > 100.0:
        return default
    return round(val, 2)


async def _read_flag(redis: Any, tenant_id: str, flag_key: str) -> Any | None:
    if redis is None:
        return None
    try:
        return await redis.get(cache_key_runtime_flag(tenant_id, flag_key))
    except Exception as exc:  # noqa: BLE001 — cache best-effort
        logger.warning("agent_thresholds: cache read fail flag=%s err=%s", flag_key, exc)
        return None


async def resolve_agent_thresholds(redis: Any, tenant_id: str = "default") -> dict[str, float]:
    """Resolve {cpu_warn, mem_warn, disk_warn} for a tenant. Always returns a bundle."""
    cpu_raw = await _read_flag(redis, tenant_id, _FLAG_CPU)
    mem_raw = await _read_flag(redis, tenant_id, _FLAG_MEM)
    disk_raw = await _read_flag(redis, tenant_id, _FLAG_DISK)
    return {
        "cpu_warn": _coerce_pct(cpu_raw, DEFAULT_CPU_WARN),
        "mem_warn": _coerce_pct(mem_raw, DEFAULT_MEM_WARN),
        "disk_warn": _coerce_pct(disk_raw, DEFAULT_DISK_WARN),
    }
