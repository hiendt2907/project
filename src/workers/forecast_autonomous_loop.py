"""Vòng lặp định kỳ: Prometheus → Prophet/fallback → ngưỡng → Telegram (admin) + Kafka (S2.3)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Any

from anomaly.prophet_forecast import forecast_backend_used, step_to_pandas_freq
from metrics.prometheus_dataframe import fetch_range_dataframe
from visualization.chart_bytes import line_chart_history_forecast_ci_png_bytes
from workers.sdk_service_tools import _duration_to_vm_window

logger = logging.getLogger(__name__)

_NS_RE = re.compile(r'namespace\s*=\s*"([^"]+)"')
_DEP_RE = re.compile(r'(?:deployment|workload|statefulset)\s*=\s*"([^"]+)"')


def _infer_labels_from_promql(promql: str) -> tuple[str, str]:
    """Best-effort extract namespace and deployment from a PromQL expression."""
    ns_m = _NS_RE.search(promql)
    dep_m = _DEP_RE.search(promql)
    return (ns_m.group(1) if ns_m else ""), (dep_m.group(1) if dep_m else "")


async def _run_one_forecast_alert(ctx: Any) -> None:
    ws = ctx.settings
    if not ws.autonomous_forecast_enabled:
        return
    cid = ws.telegram_admin_chat_id
    tg = getattr(ctx, "telegram", None)
    if tg is None or cid is None:
        return

    promql = (ws.autonomous_forecast_promql or "").strip()
    if not promql:
        return

    start, step = _duration_to_vm_window(ws.autonomous_forecast_duration)
    end = "now"
    try:
        df = await fetch_range_dataframe(
            ctx,
            promql=promql,
            start=start,
            end=end,
            step=step,
        )
    except Exception as e:
        logger.warning("autonomous_forecast fetch: %s", e)
        return

    if len(df) < 2:
        logger.info("autonomous_forecast skip: insufficient points")
        return

    pandas_freq = step_to_pandas_freq(step)
    try:
        fc, backend = forecast_backend_used(
            df,
            ws.autonomous_forecast_periods,
            freq=pandas_freq,
        )
    except Exception as e:
        logger.warning("autonomous_forecast model: %s", e)
        return

    yhat_max = float(fc["yhat"].max())
    yhat_last = float(fc["yhat"].iloc[-1])
    peak = max(yhat_max, yhat_last)
    if peak < float(ws.autonomous_forecast_threshold):
        return

    hkey = hashlib.sha256(promql.encode("utf-8")).hexdigest()[:20]
    dedupe_key = f"omni:autonomous_forecast:{hkey}"
    try:
        if await ctx.redis.get(dedupe_key):
            return
        await ctx.redis.setex(dedupe_key, int(ws.autonomous_forecast_alert_cooldown_sec), "1")
    except Exception as e:
        logger.warning("autonomous_forecast redis dedupe: %s", e)

    title = "Autonomous forecast — ngưỡng vượt"
    try:
        png = line_chart_history_forecast_ci_png_bytes(
            df["ds"].tolist(),
            df["y"].astype(float).tolist(),
            fc["ds"].tolist(),
            fc["yhat"].astype(float).tolist(),
            fc["yhat_lower"].astype(float).tolist(),
            fc["yhat_upper"].astype(float).tolist(),
            title=title,
        )
        cap = (
            f"Sếp ơi, em đoán sắp tới metric có thể vượt ngưỡng "
            f"({peak:.4g} ≥ {ws.autonomous_forecast_threshold}). "
            f"backend={backend} promql={promql[:120]}"
        )
        await tg.send_photo_bytes(int(cid), png, caption=cap[:1024])
    except Exception as e:
        logger.exception("autonomous_forecast telegram: %s", e)

    # S2.3: emit AnomalyEvent to proactive-incidents topic + set elevated watch flag.
    if not bool(getattr(ws, "forecast_proactive_integration_enabled", True)):
        return
    try:
        ns, dep = _infer_labels_from_promql(promql)
        step_seconds = _duration_to_vm_window(step)[1] if isinstance(step, str) else 60
        try:
            step_seconds = int(step.rstrip("s")) if isinstance(step, str) and step.endswith("s") else 60
        except Exception:
            step_seconds = 60
        forecast_horizon_sec = int(ws.autonomous_forecast_periods) * step_seconds
        anomaly_event = {
            "rule": "autonomous_forecast",
            "source": "forecast",
            "dr": None,
            "evt": [{
                "description": (
                    f"Forecast breach: {peak:.4g} >= {ws.autonomous_forecast_threshold}"
                ),
                "metric": promql[:120],
                "predicted_at": str(time.time() + forecast_horizon_sec),
            }],
            "z_cpu": None,
            "forecast_peak": peak,
            "forecast_threshold": float(ws.autonomous_forecast_threshold),
            "forecast_horizon_sec": forecast_horizon_sec,
            "namespace": ns,
            "deployment": dep,
            "backend": backend,
        }
        kafka = getattr(ctx, "kafka", None)
        if kafka is not None:
            topic = getattr(ws, "kafka_topic_proactive_incidents", "omni-proactive-incidents")
            await kafka.send_dict(topic, {"data": json.dumps(anomaly_event, ensure_ascii=False)})
            logger.info(
                "autonomous_forecast emit_proactive topic=%s ns=%s dep=%s peak=%.4g",
                topic, ns, dep, peak,
            )
        # Elevated watch: reduce proactive polling for this workload.
        if ns and dep:
            watch_key = f"omni:proactive:elevated:{ns}:{dep}"
            try:
                await ctx.redis.setex(watch_key, 3600, "forecast_breach")
                logger.info(
                    "autonomous_forecast elevated_watch ns=%s dep=%s key=%s", ns, dep, watch_key,
                )
            except Exception as e:
                logger.debug("autonomous_forecast elevated_watch redis fail: %s", e)
    except Exception as e:
        logger.warning("autonomous_forecast proactive_integration: %s", e)


async def autonomous_forecast_loop(ctx: Any, stop: asyncio.Event) -> None:
    await ctx.scout_ready.wait()
    ws = ctx.settings
    if not getattr(ws, "autonomous_forecast_enabled", False):
        logger.info("autonomous_forecast_loop disabled")
        return
    if ws.telegram_admin_chat_id is None:
        logger.warning("autonomous_forecast_enabled but OMNI_TELEGRAM_ADMIN_CHAT_ID unset")
        return
    interval = float(ws.autonomous_forecast_interval_sec)
    logger.info("autonomous_forecast_loop start interval_sec=%s", interval)

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            return
        try:
            await _run_one_forecast_alert(ctx)
        except Exception:
            logger.exception("autonomous_forecast tick failed")
