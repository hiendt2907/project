"""Rolling 3σ baseline for remote customer hosts, fed by agent metrics.

Why this exists: the Lane-1 resource baseline (`baseline_snapshot.py`) computes
z-scores from Prometheus PromQL. Prometheus only scrapes the ``multi-agent``
namespace — it cannot reach customer servers/VMs. The remote agent is the *only*
sensor on those hosts, and historically it emitted just a static PASS/FAIL
(threshold-based) with no notion of "normal for THIS host".

This module mirrors the in-cluster 3σ engine but sources its samples from the
agent's ``remote_system_metrics`` evidence. Each host gets its own rolling
window keyed ``3sigma:remote:{tenant}:{host}:{cpu|mem|disk}``, so anomalies are
relative to that host's own baseline rather than a one-size-fits-all cutoff.

Output z-scores are attached to the evidence envelope (``z_cpu``/``z_mem``/
``z_disk``) so downstream advisory evidence can carry a real
``3-SIGMA RESOURCE BASELINE`` block for remote hosts, matching the in-cluster lane.
"""

from __future__ import annotations

import logging
from typing import Any

from anomaly.three_sigma import ThreeSigmaGate

logger = logging.getLogger(__name__)

REMOTE_KEY_PREFIX = "3sigma:remote:"
# Customer hosts report ~once per collect_interval (default 60s). A 60-sample
# window ≈ 1h of history — enough for a stable baseline without unbounded growth.
REMOTE_WINDOW = 60
REMOTE_TTL_SEC = 7200  # 2h — survives short agent gaps; self-heals on silence.

_METRIC_KEYS: tuple[tuple[str, str], ...] = (
    ("cpu_percent", "cpu"),
    ("mem_percent", "mem"),
    ("disk_percent", "disk"),
)


def _gate(redis: Any) -> ThreeSigmaGate:
    return ThreeSigmaGate(
        redis,
        window_size=REMOTE_WINDOW,
        ttl_sec=REMOTE_TTL_SEC,
        key_prefix=REMOTE_KEY_PREFIX,
    )


def _metric_id(tenant_id: str, host: str, suffix: str) -> str:
    return f"{tenant_id}:{host}:{suffix}"


async def update_remote_host_baseline(
    redis: Any,
    *,
    tenant_id: str,
    host: str,
    fact: dict[str, Any],
) -> dict[str, float]:
    """Push cpu/mem/disk samples for a host and return any computed z-scores.

    ``fact`` is the ``extracted_fact`` block from a ``remote_system_metrics``
    envelope. Returns a dict with ``z_cpu``/``z_mem``/``z_disk`` for whichever
    metrics had enough history (>= 3 samples, std > 0). Best-effort: Redis errors
    are logged and yield an empty dict so ingest never fails on baseline math.
    """
    if redis is None or not host:
        return {}
    gate = _gate(redis)
    zscores: dict[str, float] = {}
    for fact_key, suffix in _METRIC_KEYS:
        raw = fact.get(fact_key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        try:
            _is_anom, z = await gate.observe(_metric_id(tenant_id, host, suffix), value)
        except Exception as exc:  # noqa: BLE001 — baseline is best-effort
            logger.warning("remote_baseline: observe failed host=%s metric=%s err=%r", host, suffix, exc)
            continue
        if z is not None:
            zscores[f"z_{suffix}"] = round(z, 3)
    return zscores
