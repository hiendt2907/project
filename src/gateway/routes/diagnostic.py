"""Diagnostic test endpoint — inject a synthetic test event for the UI test button.

This endpoint is used by the UI /diagnostics page's "Test lại" button to inject
a synthetic event that goes through the full diagnostic pipeline (RAG→LLM→CRAT→Telegram).
It maps frontend scenarios to domain hints that are normalized by the taxonomy.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from pkg.observability.pipeline_stages import mark_stage

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/gateway/diagnostics", tags=["diagnostic-test"])

# Map frontend scenario to (probe, alert_hint, extracted_fact, domain_hint_alias)
# domain_hint_alias is an alias that will be normalized to the desired canonical domain
SCENARIO_MAP: dict[str, tuple[str, str, dict[str, Any], str]] = {
    "service": (
        "remote_systemd_units",
        "[SIMULATOR-TEST] service failed",
        {"failed_unit": "nginx.service", "severity": "critical"},
        "services",  # normalizes to SERVICE
    ),
    "network": (
        "remote_netstat",
        "[SIMULATOR-TEST] lost listening port",
        {"port": 80, "state": "closed"},
        "net",  # normalizes to NETWORK
    ),
    "disk": (
        "remote_disk_usage",
        "[SIMULATOR-TEST] disk nearly full",
        {"disk_percent": 95, "path": "/"},
        "disk",  # normalizes to STORAGE
    ),
    "cpu": (
        "remote_system_metrics",
        "[SIMULATOR-TEST] high cpu load",
        {"cpu_percent": 95.0},
        "host",  # normalizes to OS_HOST
    ),
}


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


def _get_evidence_topic(request: Request) -> str:
    return getattr(request.app.state, "kafka_topic_evidence", "omni-diagnostic-evidence")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@router.post("/test")
async def diagnostic_test(request: Request) -> JSONResponse:
    """Inject a synthetic test event for the UI test button.

    Expected JSON body:
    {
        "scenario": "service" | "network" | "disk" | "cpu",
        "tenant_id": str (optional, defaults to "default")
    }

    Returns:
    {
        "status": "injected",
        "scenario": str,
        "trace_id": str,
        "topic": str,
        "tenant_id": str,
        "agent_id": str,
    }
    """
    try:
        body_in = await request.json()
        if not isinstance(body_in, dict):
            body_in = {}
    except Exception:
        body_in = {}

    scenario = str(body_in.get("scenario") or "").strip().lower()
    if scenario not in SCENARIO_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"invalid scenario '{scenario}'; valid: {', '.join(SCENARIO_MAP.keys())}",
        )

    probe, alert_hint, extracted_fact, domain_hint_alias = SCENARIO_MAP[scenario]

    tenant_id = str(body_in.get("tenant_id") or "default").strip()[:128] or "default"
    agent_id = str(body_in.get("agent_id") or "").strip()[:128]
    if not agent_id:
        agent_id = f"test-agent-{secrets.token_hex(3)}"

    hostname = agent_id  # use agent_id as hostname for simplicity
    now_ts = str(int(time.time()))
    trace_id = f"test-{scenario}-{secrets.token_hex(6)}"

    # Build the evidence envelope
    envelope: dict[str, Any] = {
        "trace_id": trace_id,
        "probe": probe,
        "alert_rule": "RemoteAgent_Test",
        "alert_hint": alert_hint,  # will be sanitized by agent_webhook
        "result": "FAILED",
        "extracted_fact": {
            **extracted_fact,
            "agent_id": agent_id,
            "hostname": hostname,
        },
        "raw": alert_hint,  # will be sanitized by agent_webhook
        "symptom_group": "remote_test",
        "domain": domain_hint_alias,  # alias that will be normalized by taxonomy
        "lane": "sys_resource",  # fixed lane for all test scenarios
        "stream_tags": [domain_hint_alias],
        "namespace": hostname,
        "ts": now_ts,
        "evidence_source": "RemoteAgent",
        "tenant_id": tenant_id,
        "canonical_query_snippet": json.dumps(
            {"labels": {"agent_id": agent_id, "hostname": hostname, "probe": probe}}
        ),
        # Note: _fingerprint, _dedup_count, _quality_tier, etc. are computed by agent_webhook
    }

    # Wrap envelope in data field as expected by consumers
    env = json.dumps({"data": json.dumps(envelope, ensure_ascii=False)}, ensure_ascii=False).encode("utf-8")

    # Send to Kafka
    redis = _get_redis(request)
    kafka = _get_kafka(request)
    topic = _get_evidence_topic(request)

    try:
        await kafka.send_and_wait(topic, value=env)
    except Exception as exc:
        log.error("[diagnostic-test] kafka send failed scenario=%s trace=%s err=%s", scenario, trace_id, exc)
        raise HTTPException(status_code=502, detail="kafka send failed") from exc

    # Mark INGEST stage for the trace
    _ingest_detail = f"diagnostic test scenario={scenario} target=remote lane=sys_resource topic={topic}"
    await mark_stage(redis, trace_id, "INGEST", "ok", detail=_ingest_detail, lane=domain_hint_alias)

    log.info(
        "[diagnostic-test] injected scenario=%s trace_id=%s topic=%s tenant_id=%s agent_id=%s",
        scenario,
        trace_id,
        topic,
        tenant_id,
        agent_id,
    )

    return JSONResponse(
        {
            "status": "injected",
            "scenario": scenario,
            "trace_id": trace_id,
            "topic": topic,
            "tenant_id": tenant_id,
            "agent_id": agent_id,
        }
    )