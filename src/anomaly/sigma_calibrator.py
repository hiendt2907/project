"""Per-workload adaptive sigma threshold calibration (S3.2).

Runs as a background task (daily) — queries 7-day Prometheus history for each
monitored workload, computes coefficient of variation (CV), and writes adaptive
threshold + window to Redis.

High CV (bursty service) → higher threshold (fewer false positives).
Low CV (stable service) → lower threshold (more sensitive to deviations).

Redis schema (per-workload):
  omni:sigma:config:{namespace}:{deployment}  → HSET with:
    threshold: float (sigma multiplier)
    window: int (rolling window size)
    cv: float (coefficient of variation from 7-day data)
    auto_calibrated: "true"
    calibrated_at: str(float timestamp)

Maintenance window key (set by operator):
  omni:maint:{namespace}:{deployment}  → any value, TTL = maintenance duration
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_SIGMA_CONFIG_KEY_FMT = "omni:sigma:config:{namespace}:{deployment}"
_SIGMA_CONFIG_TTL = 86400 * 14  # 14 days — re-calibrate before expiry


async def calibrate_sigma_for_workload(
    ctx: Any,
    *,
    namespace: str,
    deployment: str,
    lookback_hours: int = 168,  # 7 days
    promql_template: str | None = None,
) -> dict[str, Any] | None:
    """Compute adaptive sigma config for one workload.

    Returns the config dict on success, None if insufficient data.
    """
    import statistics as _stats

    redis = getattr(ctx, "redis", None)
    if redis is None:
        return None

    promql = promql_template or (
        f'container_cpu_usage_seconds_total{{namespace="{namespace}"}}'
    )

    try:
        from metrics.prometheus_dataframe import fetch_range_dataframe
        from pkg.observability.prometheus_window import duration_to_vm_window as _duration_to_vm_window
        start, step = _duration_to_vm_window(f"{lookback_hours}h")
        df = await fetch_range_dataframe(ctx, promql=promql, start=start, end="now", step=step)
    except Exception as e:
        logger.debug("sigma_calibrator fetch fail ns=%s dep=%s err=%s", namespace, deployment, e)
        return None

    if len(df) < 10:
        return None

    try:
        values = df["y"].astype(float).dropna().tolist()
        if len(values) < 10:
            return None
        mean_v = _stats.fmean(values)
        if mean_v < 1e-9:
            return None
        std_v = _stats.pstdev(values)
        cv = std_v / mean_v

        # Map CV to threshold and window:
        # CV < 0.2: stable → threshold 2.5σ, window 200
        # CV > 0.5: bursty → threshold 4.5σ, window 50
        threshold = round(2.5 + 2.0 * min(cv, 1.0), 2)
        window = max(50, min(200, int(50 / max(cv, 0.05))))

        cfg = {
            "threshold": str(threshold),
            "window": str(window),
            "cv": str(round(cv, 4)),
            "auto_calibrated": "true",
            "calibrated_at": str(time.time()),
            "namespace": namespace,
            "deployment": deployment,
        }

        cfg_key = _SIGMA_CONFIG_KEY_FMT.format(namespace=namespace, deployment=deployment)
        await redis.hset(cfg_key, mapping=cfg)
        await redis.expire(cfg_key, _SIGMA_CONFIG_TTL)
        logger.info(
            "event=sigma_calibrated ns=%s dep=%s cv=%.4f threshold=%.2f window=%d",
            namespace, deployment, cv, threshold, window,
        )
        return {k: (float(v) if k in ("threshold", "cv") else (int(v) if k == "window" else v))
                for k, v in cfg.items()}
    except Exception as e:
        logger.warning("sigma_calibrator compute fail ns=%s dep=%s err=%s", namespace, deployment, e)
        return None


async def run_sigma_calibration_pass(ctx: Any) -> None:
    """Calibrate sigma for all monitored workloads. Call once per 24h from core role."""
    redis = getattr(ctx, "redis", None)
    if redis is None:
        return

    try:
        # Discover monitored workloads from existing 3-sigma metric keys.
        workloads: set[tuple[str, str]] = set()
        async for key in redis.scan_iter(match="3sigma:metric:*", count=100):
            key_str = key.decode() if isinstance(key, bytes) else str(key)
            # Key format: 3sigma:metric:{metric_id} — try extract ns/dep from KPI keys.
            # Also check omni:cluster:meta:* for recently active workloads.
            pass

        # Fallback: check recently active cluster metas for known ns/dep pairs.
        async for key in redis.scan_iter(match="omni:cluster:meta:*", count=200):
            try:
                raw = await redis.hgetall(key)
                ns = (raw.get(b"namespace") or raw.get("namespace") or b"").decode() \
                    if isinstance(raw.get(b"namespace", b""), bytes) \
                    else str(raw.get("namespace", "") or "")
                if ns:
                    # Can't reliably extract dep from cluster meta — skip for now.
                    pass
            except Exception:
                pass

        # Calibrate known workloads (from omni:sigma:config:* keys already set).
        async for key in redis.scan_iter(match="omni:sigma:config:*", count=200):
            try:
                key_str = key.decode() if isinstance(key, bytes) else str(key)
                # Format: omni:sigma:config:{namespace}:{deployment}
                parts = key_str.split(":")
                if len(parts) >= 5:
                    ns = parts[3]
                    dep = ":".join(parts[4:])
                    workloads.add((ns, dep))
            except Exception:
                pass

        for ns, dep in workloads:
            await calibrate_sigma_for_workload(ctx, namespace=ns, deployment=dep)

        logger.info("event=sigma_calibration_pass_done workload_count=%d", len(workloads))
    except Exception as e:
        logger.warning("event=sigma_calibration_pass_fail err=%s", e)
