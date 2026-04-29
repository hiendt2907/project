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
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as aioredis
from aiokafka import AIOKafkaProducer
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from gateway.trace_context import install_gateway_trace_logging, pop_gateway_trace_id, push_gateway_trace_id

logger = logging.getLogger(__name__)


def _json_with_trace(content: dict[str, Any], *, trace_id: str, status_code: int = 200) -> JSONResponse:
    """Mọi response webhook đều có trace_id trong body + header để correlate với log / downstream."""
    return JSONResponse(
        content=content,
        status_code=status_code,
        headers={"X-Omni-Trace-Id": trace_id},
    )

# ─── Config from env ──────────────────────────────────────────────────────────
import hashlib
import hmac as _hmac
import os
import re

from pydantic import BaseModel, Field
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Depends, Security

_RE_TOPIC = re.compile(r"^[a-zA-Z0-9._-]+$")
# Client-supplied trace (header X-Omni-Trace-Id or query trace_id): alphanumeric + _ - , length 8–128.
_TRACE_ID_CLIENT = re.compile(r"^[a-zA-Z0-9_-]{8,128}$")


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
# Zero-Trust: HMAC-SHA256 webhook signature (lab: empty = skip; prod: set via K8s Secret omni-gateway-secret)
_WEBHOOK_SECRET: bytes = (os.getenv("OMNI_GATEWAY_WEBHOOK_SECRET") or "").strip().encode()


class PrometheusAlert(BaseModel):
    alertname: str = Field(default="")
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    status: str = Field(default="firing")
    startsAt: str = Field(default="")
    endsAt: str = Field(default="")
    generatorURL: str = Field(default="")


class PrometheusWebhookBody(BaseModel):
    receiver: str = Field(default="")
    status: str = Field(default="")
    alerts: list[PrometheusAlert] = Field(default_factory=list)
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str = Field(default="")
    version: str = Field(default="4")
    groupKey: str = Field(default="")
    truncatedAlerts: int = Field(default=0)


def _str_header(request: Request, name: str) -> str | None:
    """Safe header read: ignore MagicMock / non-str (tests)."""
    try:
        h = request.headers
        if h is None or not hasattr(h, "get"):
            return None
        v = h.get(name)
        return v.strip() if isinstance(v, str) else None
    except Exception:
        return None


def _str_query(request: Request, name: str) -> str | None:
    try:
        qp = request.query_params
        if qp is None or not hasattr(qp, "get"):
            return None
        v = qp.get(name)
        return v.strip() if isinstance(v, str) else None
    except Exception:
        return None


def _pick_valid_client_trace_id(header_val: str | None, query_val: str | None) -> str | None:
    """First valid wins: header ``X-Omni-Trace-Id``, then ``trace_id`` query param."""
    for raw in (header_val, query_val):
        if not raw:
            continue
        s = raw.strip()
        if _TRACE_ID_CLIENT.fullmatch(s):
            return s
    return None


def _resolve_prometheus_trace_id(request: Request) -> str:
    hv = _str_header(request, "x-omni-trace-id")
    qv = _str_query(request, "trace_id")
    picked = _pick_valid_client_trace_id(hv, qv)
    if picked:
        logger.info("[GATEWAY] trace_id=honor_client id=%s", picked)
        return picked
    tid = f"gw-prom-{uuid.uuid4().hex[:12]}"
    logger.info("[GATEWAY] trace_id=generated id=%s", tid)
    return tid


def _verify_hmac_signature(request: Request, raw_body: bytes) -> bool:
    """Verify X-Hub-Signature-256 header against OMNI_GATEWAY_WEBHOOK_SECRET.
    Returns True unconditionally when secret is not configured (lab mode).
    """
    if not _WEBHOOK_SECRET:
        return True
    sig_header = (
        _str_header(request, "x-hub-signature-256")
        or _str_header(request, "x-omni-signature")
    )
    if not sig_header or not sig_header.startswith("sha256="):
        return False
    expected = _hmac.new(_WEBHOOK_SECRET, raw_body, hashlib.sha256).hexdigest()
    return _hmac.compare_digest(sig_header[7:], expected)


_bearer = HTTPBearer(auto_error=False)


async def _require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    """API key guard for sensitive operational endpoints. Skip when key not configured (lab mode)."""
    key = os.getenv("OMNI_GATEWAY_API_KEY", "").strip()
    if not key:
        return
    if credentials is None or not _hmac.compare_digest(
        credentials.credentials.encode(), key.encode()
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


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
    install_gateway_trace_logging()
    if not _WEBHOOK_SECRET:
        logger.warning(
            "omni-gateway: OMNI_GATEWAY_WEBHOOK_SECRET not set — webhook signature verification DISABLED (lab mode)"
        )
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


class GatewayTraceMiddleware(BaseHTTPMiddleware):
    """Sau handler: một dòng log có trace_id (request.state) — bổ sung cho access log Uvicorn không có trace."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        tid = getattr(request.state, "trace_id", None)
        if tid and request.url.path.startswith("/webhook"):
            logger.info(
                "[GATEWAY][%s] http_done path=%s status=%s",
                tid,
                request.url.path,
                response.status_code,
            )
        return response


app.add_middleware(GatewayTraceMiddleware)

from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST, REGISTRY

def _get_or_create_counter(name: str, doc: str, labels: list[str]) -> Counter:
    try:
        return Counter(name, doc, labels)
    except ValueError:
        return REGISTRY._names_to_collectors.get(name)  # type: ignore[return-value]

gw_requests = _get_or_create_counter("omni_gateway_requests_total", "Total requests received by gateway", ["status"])

@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/healthz")
async def health():
    return {"status": "ok", "rate_limit_tps": RATE_LIMIT_TPS}


@app.post("/webhook/prometheus")
async def prometheus_webhook(request: Request) -> JSONResponse:
    """Nhận Alert từ Prometheus/Alertmanager → produce Kafka topic ``OMNI_KAFKA_TOPIC_ALERTS``."""
    # Trace: optional ``X-Omni-Trace-Id`` / ``?trace_id=`` (validated); else ``gw-prom-…``.
    trace_id = _resolve_prometheus_trace_id(request)
    request.state.trace_id = trace_id
    tok_gw = push_gateway_trace_id(trace_id)
    try:
        return await _prometheus_webhook_body(request, trace_id)
    finally:
        pop_gateway_trace_id(tok_gw)


async def _prometheus_webhook_body(request: Request, trace_id: str) -> JSONResponse:
    logger.info("[GATEWAY][%s] webhook_prometheus enter", trace_id)

    # ── 1. Rate Limiting (Token Bucket) ──────────────────────────────────────
    assert _rate_semaphore is not None
    acquired = _rate_semaphore._value > 0
    if acquired:
        await _rate_semaphore.acquire()
    else:
        logger.warning("[GATEWAY][%s] Rate limit exceeded (%d TPS). Dropping request.", trace_id, RATE_LIMIT_TPS)
        gw_requests.labels(status="429_rate_limit").inc()
        return _json_with_trace(
            {"error": "Too Many Requests", "detail": f"Rate limit {RATE_LIMIT_TPS} TPS exceeded.", "trace_id": trace_id},
            trace_id=trace_id,
            status_code=429,
        )

    try:
        # ── 2. Circuit Breaker Backpressure Check ─────────────────────────────
        assert _redis is not None
        cb_flag = await _redis.get(CB_KEY)
        if str(cb_flag).strip() == "1":
            logger.warning("[GATEWAY][%s] Circuit Breaker ACTIVE. Rejecting inbound alert.", trace_id)
            gw_requests.labels(status="503_circuit_breaker").inc()
            return _json_with_trace(
                {
                    "error": "Service Unavailable",
                    "detail": "Worker circuit breaker is active. Retry later.",
                    "trace_id": trace_id,
                },
                trace_id=trace_id,
                status_code=503,
            )

        # ── 3. Parse & Enqueue ───────────────────────────────────────────────
        raw_body = await request.body()

        if not _verify_hmac_signature(request, raw_body):
            logger.warning("[GATEWAY][%s] Webhook signature verification failed — rejecting.", trace_id)
            gw_requests.labels(status="401_invalid_signature").inc()
            return _json_with_trace(
                {"error": "Unauthorized", "detail": "Invalid or missing webhook signature.", "trace_id": trace_id},
                trace_id=trace_id,
                status_code=401,
            )

        try:
            body: Any = PrometheusWebhookBody.model_validate_json(raw_body).model_dump()
        except Exception:
            try:
                body = json.loads(raw_body) if raw_body else {}
            except Exception:
                body = {}

        if SILENCE_CHAOS_LAB and _is_chaos_lab_prometheus_webhook(body):
            logger.info("[GATEWAY][%s] Dropped chaos-lab webhook (OMNI_GATEWAY_SILENCE_CHAOS_LAB=1)", trace_id)
            gw_requests.labels(status="200_dropped_chaos_lab").inc()
            return _json_with_trace(
                {"status": "dropped", "reason": "chaos_lab_silenced", "trace_id": trace_id},
                trace_id=trace_id,
                status_code=200,
            )

        payload = {
            "source": "prometheus",
            "trace_id": trace_id,
            "received_at": time.time(),
            "data": body,
        }

        assert _kafka is not None
        env = json.dumps({"data": json.dumps(payload, ensure_ascii=False)}, ensure_ascii=False).encode("utf-8")
        await _kafka.send_and_wait(KAFKA_TOPIC_ALERTS, value=env)
        logger.info("[GATEWAY][%s] kafka_enqueued topic=%s", trace_id, KAFKA_TOPIC_ALERTS)
        gw_requests.labels(status="200_ok").inc()
        return _json_with_trace({"status": "queued", "trace_id": trace_id}, trace_id=trace_id, status_code=200)

    except Exception as e:
        logger.error("[GATEWAY][%s] Internal error: %s", trace_id, e)
        raise HTTPException(status_code=500, detail={"message": str(e), "trace_id": trace_id}) from e


@app.get("/metrics/circuit_breaker")
async def cb_status(_: None = Depends(_require_api_key)):
    """Quick status endpoint để Gateway tự expose trạng thái mạch."""
    assert _redis is not None
    flag = await _redis.get(CB_KEY)
    active = str(flag).strip() == "1"
    return {"circuit_breaker_active": active}
