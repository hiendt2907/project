"""Temporal Incident Recurrence Detection (S3.4).

Detects periodically recurring incidents (e.g., "every 24h at 2am due to cron job")
by tracking incident timestamps per pattern and computing interval statistics.

Redis schema:
  omni:temporal:ts:{pattern_key}        → ZSET score=timestamp, member=str(timestamp)
  omni:temporal:scheduled_predictions   → ZSET score=emit_at, member=JSON prediction

Background loop (core role, every 60s):
  Check scheduled_predictions ZSET for items with score <= now → emit AnomalyEvent to Kafka.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from typing import Any

logger = logging.getLogger(__name__)

_TS_KEY_FMT = "omni:temporal:ts:{pattern_key}"
_SCHEDULED_KEY = "omni:temporal:scheduled_predictions"
_MAX_KEPT_TIMESTAMPS = 30
_MIN_INCIDENTS_FOR_DETECTION = 5
_RECURRENCE_CV_THRESHOLD = 0.2  # CV < 20% → regular recurrence
_CONFIDENCE_THRESHOLD = 0.8
_LEAD_TIME_FRACTION = 0.1      # Emit prediction 10% of mean_interval before expected time
_MAX_LEAD_TIME_SEC = 1800      # Never emit more than 30 min early


async def record_incident_timestamp(
    redis: Any,
    *,
    pattern_key: str,
    timestamp: float | None = None,
) -> None:
    """Store incident occurrence timestamp for pattern recurrence analysis."""
    if redis is None or not pattern_key:
        return
    ts = timestamp if timestamp is not None else time.time()
    key = _TS_KEY_FMT.format(pattern_key=pattern_key)
    try:
        await redis.zadd(key, {str(ts): ts})
        # Keep only the most recent MAX_KEPT_TIMESTAMPS incidents.
        await redis.zremrangebyrank(key, 0, -(1 + _MAX_KEPT_TIMESTAMPS))
        await redis.expire(key, 86400 * 90)
    except Exception as e:
        logger.debug("temporal record_ts fail pattern=%s err=%s", pattern_key, e)


async def detect_recurrence(
    redis: Any,
    *,
    pattern_key: str,
) -> dict[str, Any] | None:
    """Analyse incident timestamps → detect regular recurrence.

    Returns recurrence info dict or None if pattern not detected.
    """
    if redis is None or not pattern_key:
        return None
    key = _TS_KEY_FMT.format(pattern_key=pattern_key)
    try:
        raw = await redis.zrangebyscore(key, "-inf", "+inf", withscores=False)
    except Exception as e:
        logger.debug("temporal detect_recurrence redis fail pattern=%s err=%s", pattern_key, e)
        return None

    timestamps = sorted(float(r.decode() if isinstance(r, bytes) else r) for r in raw)
    if len(timestamps) < _MIN_INCIDENTS_FOR_DETECTION:
        return None

    intervals = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
    mean_interval = statistics.fmean(intervals)
    if mean_interval < 60:  # Intervals under 1 minute are noise.
        return None

    stdev_interval = statistics.pstdev(intervals)
    cv = stdev_interval / mean_interval if mean_interval > 0 else 1.0

    if cv >= _RECURRENCE_CV_THRESHOLD:
        return None  # Irregular — not a predictable pattern.

    confidence = round(1.0 - cv, 3)
    next_predicted = timestamps[-1] + mean_interval
    return {
        "pattern_key": pattern_key,
        "mean_interval_sec": round(mean_interval, 1),
        "next_predicted_at": round(next_predicted, 1),
        "confidence": confidence,
        "incident_count": len(timestamps),
        "cv": round(cv, 4),
    }


async def maybe_schedule_prediction(
    redis: Any,
    *,
    pattern_key: str,
    kafka_topic: str = "omni-proactive-incidents",
) -> bool:
    """Check for recurrence and schedule a predictive AnomalyEvent if confident enough.

    Returns True if a prediction was scheduled.
    """
    recurrence = await detect_recurrence(redis, pattern_key=pattern_key)
    if recurrence is None:
        return False
    if recurrence["confidence"] < _CONFIDENCE_THRESHOLD:
        return False

    lead_time = min(
        _MAX_LEAD_TIME_SEC,
        recurrence["mean_interval_sec"] * _LEAD_TIME_FRACTION,
    )
    emit_at = recurrence["next_predicted_at"] - lead_time

    if emit_at <= time.time():
        return False  # Already past the emit time — skip this cycle.

    prediction = {
        **recurrence,
        "emit_at": emit_at,
        "kafka_topic": kafka_topic,
        "source": "temporal_pattern_matcher",
        "rule": "temporal_prediction",
    }

    try:
        await redis.zadd(_SCHEDULED_KEY, {json.dumps(prediction, ensure_ascii=False): emit_at})
        logger.info(
            "event=temporal_prediction_scheduled pattern=%s emit_at=%.0f confidence=%.3f",
            pattern_key, emit_at, recurrence["confidence"],
        )
        return True
    except Exception as e:
        logger.debug("temporal schedule fail pattern=%s err=%s", pattern_key, e)
        return False


async def emit_due_predictions(ctx: Any) -> int:
    """Background loop tick: emit predictions whose emit_at has passed.

    Returns count of emitted predictions.
    """
    redis = getattr(ctx, "redis", None)
    kafka = getattr(ctx, "kafka", None)
    if redis is None:
        return 0

    now = time.time()
    emitted = 0
    try:
        raw_items = await redis.zrangebyscore(_SCHEDULED_KEY, "-inf", now)
    except Exception as e:
        logger.debug("temporal emit_due redis fail err=%s", e)
        return 0

    for raw in raw_items:
        try:
            item_str = raw.decode() if isinstance(raw, bytes) else str(raw)
            prediction = json.loads(item_str)
            kafka_topic = prediction.get("kafka_topic", "omni-proactive-incidents")
            anomaly_event = {
                "rule": prediction.get("rule", "temporal_prediction"),
                "source": prediction.get("source", "temporal_pattern_matcher"),
                "dr": None,
                "evt": [{
                    "description": (
                        f"Predicted recurrence: pattern={prediction.get('pattern_key')} "
                        f"interval={prediction.get('mean_interval_sec', 0):.0f}s "
                        f"confidence={prediction.get('confidence', 0):.2%}"
                    ),
                }],
                "mean_interval_sec": prediction.get("mean_interval_sec"),
                "confidence": prediction.get("confidence"),
                "pattern_key": prediction.get("pattern_key"),
                "next_predicted_at": prediction.get("next_predicted_at"),
            }
            if kafka is not None:
                await kafka.send_dict(kafka_topic, {"data": json.dumps(anomaly_event, ensure_ascii=False)})
                emitted += 1
                logger.info(
                    "event=temporal_prediction_emitted pattern=%s topic=%s",
                    prediction.get("pattern_key"), kafka_topic,
                )
            # Remove from scheduled set.
            await redis.zrem(_SCHEDULED_KEY, raw)
        except Exception as e:
            logger.warning("temporal emit_due item fail err=%s", e)
            try:
                await redis.zrem(_SCHEDULED_KEY, raw)
            except Exception:
                pass

    return emitted
