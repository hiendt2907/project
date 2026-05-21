"""POST /agent/v1/push — remote Linux agent evidence ingestion endpoint.

Auth: Redis hash `omni:agent:tenant:{api_key}` with fields `tenant_id` and optional
`allowed_agents` (JSON list or "*").  Result cached 60 s per key.

Fan-out:
  metrics + SYS_RESOURCE → omni-diagnostic-evidence
  log_event + SYS_HARD_FAIL|APP_HTTP → omni-diagnostic-evidence
  alert + SIEM_SECURITY → omni-alerts
  custom_check → omni-diagnostic-evidence
  default → omni-diagnostic-evidence
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from gateway.schemas.agent_envelope import AgentEvidenceEnvelope, EvidenceType, StreamTag

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent/v1", tags=["remote-agent-push"])

# Redis key prefixes
_AUTH_PREFIX = "omni:agent:tenant:"
_AUTH_CACHE_PREFIX = "omni:agentauth:cache:"
_AUTH_CACHE_TTL = 60  # seconds
_REGISTRY_PREFIX = "omni:remote_agent:registry:"
_REGISTRY_TTL = 300  # seconds
_EPS_PREFIX = "omni:remote_agent:eps:"
_EPS_WINDOW_MS = 60_000  # 60 s rolling window


def _get_redis(request: Request) -> Any:
    r = getattr(request.app.state, "redis", None)
    if r is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return r


def _get_kafka(request: Request) -> Any:
    k = getattr(request.app.state, "kafka", None)
    if k is None:
        raise HTTPException(status_code=503, detail="Kafka not available")
    return k


def _get_topic_evidence(request: Request) -> str:
    return getattr(request.app.state, "kafka_topic_evidence", "omni-diagnostic-evidence")


def _get_topic_alerts(request: Request) -> str:
    return getattr(request.app.state, "kafka_topic_alerts", "omni-alerts")


async def _resolve_agent_auth(request: Request) -> tuple[str, str]:
    """Validate agent auth headers, return (tenant_id, agent_id).

    Raises HTTPException 401/403/503 on failure.
    """
    redis = _get_redis(request)

    api_key: str | None = request.headers.get("X-Omni-API-Key")
    agent_id_header: str | None = request.headers.get("X-Omni-Agent-ID")
    tenant_id_header: str | None = request.headers.get("X-Omni-Tenant-ID")

    if not api_key:
        raise HTTPException(status_code=401, detail="Missing X-Omni-API-Key header")

    # Try cache first
    cache_key = f"{_AUTH_CACHE_PREFIX}{api_key}"
    cached_raw = await redis.get(cache_key)
    if cached_raw:
        record: dict[str, Any] = json.loads(cached_raw)
    else:
        # Look up Redis hash
        record = await redis.hgetall(f"{_AUTH_PREFIX}{api_key}")
        if not record or "tenant_id" not in record:
            raise HTTPException(status_code=401, detail="Invalid API key")
        # Cache the record
        await redis.setex(cache_key, _AUTH_CACHE_TTL, json.dumps(record))

    db_tenant_id: str = record["tenant_id"]

    # Validate tenant_id header if provided
    if tenant_id_header and tenant_id_header != db_tenant_id:
        raise HTTPException(status_code=403, detail="Tenant ID mismatch")

    # Validate agent_id against allowed_agents
    allowed_agents_raw = record.get("allowed_agents", "*")
    if allowed_agents_raw != "*":
        try:
            allowed: list[str] = json.loads(allowed_agents_raw) if isinstance(allowed_agents_raw, str) else allowed_agents_raw
        except (json.JSONDecodeError, TypeError):
            allowed = [allowed_agents_raw]
        if agent_id_header and agent_id_header not in allowed:
            raise HTTPException(status_code=403, detail="Agent ID not authorized")

    return db_tenant_id, agent_id_header or ""


def _pick_topic(envelope: AgentEvidenceEnvelope, topic_evidence: str, topic_alerts: str) -> str:
    """Determine Kafka topic from evidence_type + stream_tags."""
    tags = set(envelope.stream_tags)
    et = envelope.evidence_type

    if et == EvidenceType.metrics and StreamTag.SYS_RESOURCE in tags:
        return topic_evidence
    if et == EvidenceType.log_event and (StreamTag.SYS_HARD_FAIL in tags or StreamTag.APP_HTTP in tags):
        return topic_evidence
    if et == EvidenceType.alert and StreamTag.SIEM_SECURITY in tags:
        return topic_alerts
    if et == EvidenceType.custom_check:
        return topic_evidence
    return topic_evidence


async def _register_heartbeat(redis: Any, envelope: AgentEvidenceEnvelope, tenant_id: str) -> None:
    """Write agent heartbeat and EPS tracking to Redis."""
    agent_id = envelope.agent_id
    now_ms = int(time.time() * 1000)
    now_s = time.time()

    # Registry heartbeat
    registry_key = f"{_REGISTRY_PREFIX}{agent_id}"
    registry_val = json.dumps({
        "agent_id": agent_id,
        "tenant_id": tenant_id,
        "source_type": envelope.source_type.value,
        "last_seen": now_s,
        "agent_version": envelope.agent_version,
    })
    await redis.setex(registry_key, _REGISTRY_TTL, registry_val)

    # EPS tracking: ZADD then trim to last 60 s
    eps_key = f"{_EPS_PREFIX}{agent_id}"
    await redis.zadd(eps_key, {envelope.trace_id: now_ms})
    cutoff_ms = now_ms - _EPS_WINDOW_MS
    await redis.zremrangebyscore(eps_key, "-inf", cutoff_ms)


@router.post("/push")
async def agent_push(
    body: AgentEvidenceEnvelope,
    request: Request,
) -> JSONResponse:
    """Ingest an evidence envelope from a remote agent."""
    tenant_id, _ = await _resolve_agent_auth(request)

    redis = _get_redis(request)
    kafka = _get_kafka(request)
    topic_evidence = _get_topic_evidence(request)
    topic_alerts = _get_topic_alerts(request)

    topic = _pick_topic(body, topic_evidence, topic_alerts)

    # Build Kafka envelope matching gateway conventions.
    # evidence_source must match agent_webhook.py:315 for evidence_consumer routing.
    kafka_payload = json.dumps(
        {
            "source": "remote_agent",
            "evidence_source": "RemoteAgent",
            "tenant_id": body.tenant_id,
            "agent_id": body.agent_id,
            "trace_id": body.trace_id,
            "received_at": time.time(),
            "stream_tags": [t.value for t in body.stream_tags],
            "data": body.model_dump(),
        },
        ensure_ascii=False,
    )
    envelope_msg = {"data": kafka_payload}
    await kafka.send_and_wait(topic, value=json.dumps(envelope_msg, ensure_ascii=False).encode())

    # Register heartbeat
    await _register_heartbeat(redis, body, tenant_id)

    logger.info(
        "[AGENT-PUSH] trace_id=%s tenant=%s agent=%s topic=%s",
        body.trace_id,
        tenant_id,
        body.agent_id,
        topic,
    )

    return JSONResponse(
        content={"status": "accepted", "trace_id": body.trace_id, "topic": topic},
        status_code=202,
    )
