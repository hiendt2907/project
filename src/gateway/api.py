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
KAFKA_TOPIC_EVIDENCE = (os.getenv("OMNI_KAFKA_TOPIC_DIAGNOSTIC_EVIDENCE") or "omni-diagnostic-evidence").strip()
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
    alerts: list[PrometheusAlert] = Field(default_factory=list, max_length=1000)
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

from gateway.tenant_context import TenantContext  # noqa: E402


def _parse_tenant_apikeys() -> dict[str, str]:
    """Parse OMNI_TENANT_APIKEYS=tid1:key1,tid2:key2 → {key: tenant_id}."""
    raw = os.getenv("OMNI_TENANT_APIKEYS", "").strip()
    if not raw:
        return {}
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" in pair:
            tid, _, key = pair.partition(":")
            tid, key = tid.strip(), key.strip()
            if tid and key:
                result[key] = tid
    return result


async def _require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> TenantContext:
    """API key guard. Injects TenantContext into request.state.tenant.

    Per-tenant keys: OMNI_TENANT_APIKEYS=tid1:key1,tid2:key2
    Admin keys: OMNI_ADMIN_API_KEYS (comma-separated).
    Master key: OMNI_GATEWAY_API_KEY (backward compat, treated as admin).
    Lab mode (no key configured, non-prod): injects is_admin=True.
    """
    master_key = os.getenv("OMNI_GATEWAY_API_KEY", "").strip()
    tenant_keys = _parse_tenant_apikeys()
    admin_keys_raw = os.getenv("OMNI_ADMIN_API_KEYS", "").strip()
    admin_keys = frozenset(k.strip() for k in admin_keys_raw.split(",") if k.strip())

    has_any_key = bool(master_key or tenant_keys or admin_keys)
    if not has_any_key:
        if os.getenv("OMNI_ENV_MODE", "prod").strip().lower() == "prod":
            raise HTTPException(status_code=503, detail="Gateway API key not configured")
        ctx = TenantContext(tenant_id="lab", is_admin=True)
        request.state.tenant = ctx
        return ctx

    incoming = credentials.credentials if credentials else ""

    # Check per-tenant keys first
    if tenant_keys and incoming:
        for key, tid in tenant_keys.items():
            if _hmac.compare_digest(incoming.encode(), key.encode()):
                is_admin = bool(admin_keys and any(
                    _hmac.compare_digest(incoming.encode(), ak.encode()) for ak in admin_keys
                ))
                ctx = TenantContext(tenant_id=tid, is_admin=is_admin)
                request.state.tenant = ctx
                return ctx

    # Check explicit admin keys
    if admin_keys and incoming:
        for ak in admin_keys:
            if _hmac.compare_digest(incoming.encode(), ak.encode()):
                ctx = TenantContext(tenant_id="admin", is_admin=True)
                request.state.tenant = ctx
                return ctx

    # Backward compat: master key (OMNI_GATEWAY_API_KEY)
    if master_key and incoming:
        if _hmac.compare_digest(incoming.encode(), master_key.encode()):
            is_admin = not admin_keys or any(
                _hmac.compare_digest(incoming.encode(), ak.encode()) for ak in admin_keys
            )
            ctx = TenantContext(tenant_id="default", is_admin=is_admin)
            request.state.tenant = ctx
            return ctx

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
# Token Bucket: integer counter — asyncio single-threaded, no lock needed.
_rate_tokens: int = 0
_token_refill_task: asyncio.Task | None = None


async def _refill_tokens() -> None:
    """Reset token bucket to RATE_LIMIT_TPS every second."""
    global _rate_tokens
    while True:
        await asyncio.sleep(1.0)
        _rate_tokens = RATE_LIMIT_TPS


def _build_redis_client() -> aioredis.Redis:
    logger.info("omni-gateway using Redis standalone URL")
    return aioredis.Redis.from_url(REDIS_URL, decode_responses=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis, _kafka, _rate_tokens, _token_refill_task
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
    global _rate_tokens
    _rate_tokens = RATE_LIMIT_TPS
    _token_refill_task = asyncio.create_task(_refill_tokens())
    logger.info(
        "omni-gateway started. rate_limit=%d tps kafka_topic=%s bootstrap=%s",
        RATE_LIMIT_TPS,
        KAFKA_TOPIC_ALERTS,
        KAFKA_BOOTSTRAP,
    )
    app.state.redis = _redis
    app.state.kafka = _kafka
    app.state.kafka_topic_evidence = KAFKA_TOPIC_EVIDENCE
    app.state.kafka_topic_alerts = KAFKA_TOPIC_ALERTS
    yield
    if _token_refill_task:
        _token_refill_task.cancel()
    if _kafka:
        await _kafka.stop()
    if _redis:
        await _redis.aclose()
    logger.info("omni-gateway shutdown.")


app = FastAPI(title="Omni Gateway", version="1.0.0", lifespan=lifespan)

from fastapi import Depends as _Depends  # noqa: E402 (already imported above but alias for clarity)
from gateway.routes.kpi import router as _kpi_router  # noqa: E402
from gateway.routes.playbooks import router as _playbooks_router  # noqa: E402
from gateway.routes.siem import router as _siem_router  # noqa: E402
from gateway.routes.agents import router as _agents_router  # noqa: E402
from gateway.routes.autonomy import router as _autonomy_router  # noqa: E402
from gateway.routes.compliance import router as _compliance_router  # noqa: E402
from gateway.routes.agent_webhook import router as _agent_webhook_router  # noqa: E402
from gateway.routes.agent_push import router as _agent_push_router  # noqa: E402

app.include_router(_kpi_router, dependencies=[_Depends(_require_api_key)])
app.include_router(_playbooks_router, dependencies=[_Depends(_require_api_key)])
app.include_router(_siem_router, dependencies=[_Depends(_require_api_key)])
app.include_router(_agents_router, dependencies=[_Depends(_require_api_key)])
app.include_router(_autonomy_router, dependencies=[_Depends(_require_api_key)])
app.include_router(_compliance_router, dependencies=[_Depends(_require_api_key)])
app.include_router(_agent_webhook_router, dependencies=[_Depends(_require_api_key)])
app.include_router(_agent_push_router)  # agent_push has its own auth — no gateway API key guard


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

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, generate_latest

def _get_or_create_counter(name: str, doc: str, labels: list[str]) -> Counter:
    try:
        return Counter(name, doc, labels)
    except ValueError:
        return REGISTRY._names_to_collectors.get(name)  # type: ignore[return-value]


def _get_or_create_gauge(name: str, doc: str) -> Gauge:
    try:
        return Gauge(name, doc)
    except ValueError:
        return REGISTRY._names_to_collectors.get(name)  # type: ignore[return-value]


gw_requests = _get_or_create_counter("omni_gateway_requests_total", "Total requests received by gateway", ["status"])
gw_circuit_open = _get_or_create_gauge(
    "omni_gateway_circuit_open",
    "1 when Redis omni:circuit_breaker:active blocks Prometheus webhook ingest (503 path).",
)

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
    global _rate_tokens
    if _rate_tokens > 0:
        _rate_tokens -= 1
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
        try:
            gw_circuit_open.set(1.0 if str(cb_flag).strip() == "1" else 0.0)
        except Exception:
            pass
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


# ─── Forecast Matrix ──────────────────────────────────────────────────────────

import datetime as _dt

def _linear_forecast(values: list[float], *, horizon_steps: int) -> tuple[list[float], dict[str, float]]:
    """Inline linear regression — no scipy/numpy dependency in gateway image."""
    import statistics as _st
    n = len(values)
    xs = list(range(n))
    x_mean = _st.mean(xs)
    y_mean = _st.mean(values)
    ss_xy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values))
    ss_xx = sum((x - x_mean) ** 2 for x in xs)
    slope = ss_xy / ss_xx if ss_xx else 0.0
    intercept = y_mean - slope * x_mean
    y_hat = [slope * x + intercept for x in xs]
    ss_res = sum((y - yh) ** 2 for y, yh in zip(values, y_hat))
    ss_tot = sum((y - y_mean) ** 2 for y in values)
    r_squared = 1.0 - ss_res / ss_tot if ss_tot else 1.0
    pred_x_start = n
    pred_y = [slope * (pred_x_start + i) + intercept for i in range(horizon_steps)]
    return pred_y, {"slope": slope, "intercept": intercept, "r_squared": r_squared}

_FORECAST_HORIZONS_HOURS: list[float] = [1.0, 3.0, 6.0, 12.0, 24.0]
_FORECAST_RISK_THRESHOLD: float = float(os.getenv("OMNI_FORECAST_RISK_THRESHOLD", "0.9"))


class ForecastMatrixRequest(BaseModel):
    metric_name: str = Field(..., min_length=1, max_length=256)
    values: list[float] = Field(..., min_length=2, max_length=10000)
    timestamps: list[float] = Field(..., min_length=2, max_length=10000)
    step_seconds: float = Field(default=300.0, gt=0)


class ForecastHorizon(BaseModel):
    predicted: float
    slope: float
    r_squared: float
    risk: bool
    confidence: str = "ok"  # "ok" | "low" — low when r_squared below OMNI_FORECAST_MIN_R_SQUARED


class ForecastMatrixResponse(BaseModel):
    metric_name: str
    current_value: float
    step_seconds: float
    horizons: dict[str, ForecastHorizon]
    computed_at: str


@app.post("/forecast/matrix", response_model=ForecastMatrixResponse)
async def forecast_matrix(
    body: ForecastMatrixRequest,
    _: None = Depends(_require_api_key),
) -> ForecastMatrixResponse:
    """Dự báo metric tại 5 mốc thời gian (1h/3h/6h/12h/24h) bằng hồi quy tuyến tính."""
    if len(body.values) != len(body.timestamps):
        raise HTTPException(status_code=422, detail="values and timestamps must have the same length")
    if len(body.values) < 2:
        raise HTTPException(status_code=422, detail="at least 2 data points required")

    current_value = body.values[-1]
    horizons: dict[str, ForecastHorizon] = {}

    for h_hours in _FORECAST_HORIZONS_HOURS:
        horizon_steps = max(1, int(h_hours * 3600.0 / body.step_seconds))
        try:
            pred_y, meta = _linear_forecast(body.values, horizon_steps=horizon_steps)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"forecast error: {exc}") from exc

        predicted = float(pred_y[-1])
        slope = meta["slope"]
        r_squared = meta["r_squared"]
        low_confidence = bool(meta.get("low_confidence", False))
        # Risk heuristic: only escalate when model fit is reliable (r_squared >= threshold)
        risk = (
            not low_confidence
            and current_value > 0
            and predicted / current_value > _FORECAST_RISK_THRESHOLD
        ) if current_value != 0 else False

        key = f"{int(h_hours)}h" if h_hours == int(h_hours) else f"{h_hours}h"
        horizons[key] = ForecastHorizon(
            predicted=predicted,
            slope=slope,
            r_squared=r_squared,
            risk=risk,
            confidence="low" if low_confidence else "ok",
        )

    return ForecastMatrixResponse(
        metric_name=body.metric_name,
        current_value=current_value,
        step_seconds=body.step_seconds,
        horizons=horizons,
        computed_at=_dt.datetime.now(_dt.UTC).isoformat(),
    )
