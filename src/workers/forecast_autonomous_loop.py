"""Vòng lặp định kỳ: Prometheus → Prophet/fallback → ngưỡng → Telegram (admin)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from anomaly.prophet_forecast import forecast_backend_used, step_to_pandas_freq
from metrics.prometheus_dataframe import fetch_range_dataframe
from visualization.chart_bytes import line_chart_history_forecast_ci_png_bytes
from workers.sdk_service_tools import _duration_to_vm_window

logger = logging.getLogger(__name__)


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
