"""
Omni Gateway — FastAPI Ingress only (zero-trust vs worker).

Contract: nhận HTTP, produce Kafka topic ``omni-alerts`` (payload JSON bọc ``data``).
Cấm import ``src/workers/``, ``pkg/reasoning/``, ``pkg/executor/`` — chỉ FastAPI + Redis + aiokafka + metrics.

Rate limit: token bucket; backpressure: Redis ``omni:circuit_breaker:active`` trước khi produce.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis
from aiokafka import AIOKafkaProducer
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# ─── Config from env ──────────────────────────────────────────────────────────
import os
import re

_RE_TOPIC = re.compile(r"^[a-zA-Z0-9._-]+$")


def _kafka_topic_from_env() -> str:
    raw = os.getenv("OMNI_KAFKA_TOPIC_ALERTS") or os.getenv("OMNI_STREAM_INBOUND") or "omni-alerts"
    s = (raw or "").strip()
    if not s or not _RE_TOPIC.match(s):
        return "omni-alerts"
    return s


REDIS_URL = os.getenv("OMNI_REDIS_URL", "redis://redis:6379/0")
KAFKA_BOOTSTRAP = os.getenv("OMNI_KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
KAFKA_TOPIC_ALERTS = _kafka_topic_from_env()
RATE_LIMIT_TPS = int(os.getenv("OMNI_GATEWAY_RATE_LIMIT_TPS", "1000"))
CB_KEY = "omni:circuit_breaker:active"
SILENCE_CHAOS_LAB = os.getenv("OMNI_GATEWAY_SILENCE_CHAOS_LAB", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)


def _is_chaos_lab_prometheus_webhook(body: Any) -> bool:
    """True nếu payload khớp scripts/agentic_chaos_validation.py (không XADD khi bật silence)."""
    if not isinstance(body, dict):
        return False
    if body.get("receiver") == "omni-chaos-validation":
        return True
    for raw in body.get("alerts") or []:
        if not isinstance(raw, dict):
            continue
        labels = raw.get("labels")
        if isinstance(labels, dict) and labels.get("alertname") == "ChaosLabAlert":
            return True
    return False

# ─── State ────────────────────────────────────────────────────────────────────
_redis: aioredis.Redis | None = None
_kafka: AIOKafkaProducer | None = None
# Token Bucket (asyncio): giới hạn N concurrent request mỗi giây
_rate_semaphore: asyncio.Semaphore | None = None
_token_refill_task: asyncio.Task | None = None


async def _refill_tokens() -> None:
    """Refill rate semaphore every second up to RATE_LIMIT_TPS."""
    global _rate_semaphore
    while True:
        await asyncio.sleep(1.0)
        if _rate_semaphore is None:
            continue
        # Nhả tối đa RATE_LIMIT_TPS token vào bucket mỗi giây
        released = 0
        while _rate_semaphore._value < RATE_LIMIT_TPS and released < RATE_LIMIT_TPS:
            try:
                _rate_semaphore.release()
                released += 1
            except Exception:
                break


def _build_redis_client() -> aioredis.Redis:
    logger.info("omni-gateway using Redis standalone URL")
    return aioredis.Redis.from_url(REDIS_URL, decode_responses=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis, _kafka, _rate_semaphore, _token_refill_task
    _redis = _build_redis_client()
    await _redis.initialize()
    _kafka = AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP.strip(),
        enable_idempotence=True,
        acks="all",
    )
    await _kafka.start()
    # Khởi tạo với đúng RATE_LIMIT_TPS token ban đầu
    _rate_semaphore = asyncio.Semaphore(RATE_LIMIT_TPS)
    _token_refill_task = asyncio.create_task(_refill_tokens())
    logger.info(
        "omni-gateway started. rate_limit=%d tps kafka_topic=%s bootstrap=%s",
        RATE_LIMIT_TPS,
        KAFKA_TOPIC_ALERTS,
        KAFKA_BOOTSTRAP,
    )
    yield
    if _token_refill_task:
        _token_refill_task.cancel()
    if _kafka:
        await _kafka.stop()
    if _redis:
        await _redis.aclose()
    logger.info("omni-gateway shutdown.")


app = FastAPI(title="Omni Gateway", version="1.0.0", lifespan=lifespan)

from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST
gw_requests = Counter("omni_gateway_requests_total", "Total requests received by gateway", ["status"])

@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/healthz")
async def health():
    return {"status": "ok", "rate_limit_tps": RATE_LIMIT_TPS}


@app.post("/webhook/prometheus")
async def prometheus_webhook(request: Request) -> JSONResponse:
    """Nhận Alert từ Prometheus/Alertmanager → produce Kafka topic ``OMNI_KAFKA_TOPIC_ALERTS``."""
    # ── 1. Rate Limiting (Token Bucket) ──────────────────────────────────────
    assert _rate_semaphore is not None
    acquired = _rate_semaphore._value > 0
    if acquired:
        await _rate_semaphore.acquire()
    else:
        logger.warning("[GATEWAY] Rate limit exceeded (%d TPS). Dropping request.", RATE_LIMIT_TPS)
        gw_requests.labels(status="429_rate_limit").inc()
        return JSONResponse(
            status_code=429,
            content={"error": "Too Many Requests", "detail": f"Rate limit {RATE_LIMIT_TPS} TPS exceeded."},
        )

    try:
        # ── 2. Circuit Breaker Backpressure Check ─────────────────────────────
        assert _redis is not None
        cb_flag = await _redis.get(CB_KEY)
        if str(cb_flag).strip() == "1":
            logger.warning("[GATEWAY] Circuit Breaker ACTIVE. Rejecting inbound alert.")
            gw_requests.labels(status="503_circuit_breaker").inc()
            return JSONResponse(
                status_code=503,
                content={"error": "Service Unavailable", "detail": "Worker circuit breaker is active. Retry later."},
            )

        # ── 3. Parse & Enqueue ───────────────────────────────────────────────
        try:
            body: Any = await request.json()
        except Exception:
            body = {}

        if SILENCE_CHAOS_LAB and _is_chaos_lab_prometheus_webhook(body):
            logger.info("[GATEWAY] Dropped chaos-lab webhook (OMNI_GATEWAY_SILENCE_CHAOS_LAB=1)")
            gw_requests.labels(status="200_dropped_chaos_lab").inc()
            return JSONResponse(
                status_code=200,
                content={"status": "dropped", "reason": "chaos_lab_silenced"},
            )

        trace_id = f"gw-prom-{uuid.uuid4().hex[:12]}"
        payload = {
            "source": "prometheus",
            "trace_id": trace_id,
            "received_at": time.time(),
            "data": body,
        }

        assert _kafka is not None
        env = json.dumps({"data": json.dumps(payload, ensure_ascii=False)}, ensure_ascii=False).encode("utf-8")
        await _kafka.send_and_wait(KAFKA_TOPIC_ALERTS, value=env)
        logger.info("[GATEWAY] Enqueued alert trace_id=%s", trace_id)
        gw_requests.labels(status="200_ok").inc()
        return JSONResponse(status_code=200, content={"status": "queued", "trace_id": trace_id})

    except Exception as e:
        logger.error("[GATEWAY] Internal error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Token đã dùng — KHÔNG release lại (bucket refill sẽ tự làm mỗi giây)
        pass


@app.get("/metrics/circuit_breaker")
async def cb_status():
    """Quick status endpoint để Gateway tự expose trạng thái mạch."""
    assert _redis is not None
    flag = await _redis.get(CB_KEY)
    active = str(flag).strip() == "1"
    return {"circuit_breaker_active": active}
