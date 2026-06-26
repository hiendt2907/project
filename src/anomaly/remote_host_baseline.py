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
from enum import Enum
from typing import Any

from anomaly.three_sigma import ThreeSigmaGate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data Confidence Score
# ---------------------------------------------------------------------------
# Mỗi remote host có điểm 0–100 biểu thị Omni đã "học" được bao nhiêu về host đó.
# Điểm tích lũy từ metric samples, doc upload, incident history. Decay -5/ngày offline.
# Điểm quyết định autonomy level hiệu quả = min(tenant_tier, confidence_level).

_CONFIDENCE_KEY_PREFIX = "omni:3sigma:confidence:"
_CONFIDENCE_TTL = 30 * 86400  # 30 ngày
_CONFIDENCE_MAX = 100
_CONFIDENCE_MIN = 0


class ConfidenceLevel(str, Enum):
    STATIC_GUARD = "STATIC_GUARD"   # 0–24: chỉ hard-threshold, không RAG/LLM
    LEARNING = "LEARNING"           # 25–49: thu thập dữ liệu
    ASSISTED = "ASSISTED"           # 50–74: RAG lookup khi có sự cố
    AUTONOMOUS = "AUTONOMOUS"       # 75–100: diagnostics đầy đủ


_LEVEL_THRESHOLDS: list[tuple[int, ConfidenceLevel]] = [
    (75, ConfidenceLevel.AUTONOMOUS),
    (50, ConfidenceLevel.ASSISTED),
    (25, ConfidenceLevel.LEARNING),
    (0, ConfidenceLevel.STATIC_GUARD),
]


def score_to_level(score: int) -> ConfidenceLevel:
    for threshold, level in _LEVEL_THRESHOLDS:
        if score >= threshold:
            return level
    return ConfidenceLevel.STATIC_GUARD


def _confidence_key(tenant_id: str, host: str) -> str:
    return f"{_CONFIDENCE_KEY_PREFIX}{tenant_id}:{host}"


async def get_confidence_score(redis: Any, *, tenant_id: str, host: str) -> int:
    if redis is None:
        return 0
    try:
        raw = await redis.get(_confidence_key(tenant_id, host))
        return int(raw) if raw is not None else 0
    except Exception as exc:
        logger.warning("confidence_score: get failed tenant=%s host=%s err=%r", tenant_id, host, exc)
        return 0


async def add_confidence(
    redis: Any,
    *,
    tenant_id: str,
    host: str,
    delta: int,
    notify_fn: Any = None,
) -> int:
    """Tăng điểm confidence delta, giữ trong [0, 100]. Trả về điểm mới.

    notify_fn(old_level, new_level, tenant_id, host) — gọi khi crossing threshold,
    dùng để gửi Telegram. Nếu None thì bỏ qua.
    """
    if redis is None:
        return 0
    key = _confidence_key(tenant_id, host)
    try:
        old_score = int((await redis.get(key)) or 0)
        new_score = max(_CONFIDENCE_MIN, min(_CONFIDENCE_MAX, old_score + delta))
        await redis.set(key, str(new_score), ex=_CONFIDENCE_TTL)
        if notify_fn is not None:
            old_level = score_to_level(old_score)
            new_level = score_to_level(new_score)
            if old_level != new_level:
                try:
                    await notify_fn(old_level, new_level, tenant_id, host)
                except Exception as exc:
                    logger.warning("confidence: notify failed host=%s err=%r", host, exc)
        return new_score
    except Exception as exc:
        logger.warning("confidence: add failed tenant=%s host=%s delta=%d err=%r", tenant_id, host, delta, exc)
        return 0


async def decay_confidence(
    redis: Any,
    *,
    tenant_id: str,
    host: str,
    decay: int = 5,
) -> int:
    """Trừ điểm confidence (gọi hàng ngày khi agent offline)."""
    return await add_confidence(redis, tenant_id=tenant_id, host=host, delta=-decay)


async def get_confidence_level(redis: Any, *, tenant_id: str, host: str) -> ConfidenceLevel:
    score = await get_confidence_score(redis, tenant_id=tenant_id, host=host)
    return score_to_level(score)

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
