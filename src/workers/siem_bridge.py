"""
SIEM Bridge Worker — reads FinGuard Redis Streams, publishes to Omni Kafka.

Flow:
  finguard-customer/redis  stream:actionable_incidents (XREADGROUP)
    → translate FinGuard incident → Omni alert envelope
    → kafka multi-agent/omni-alerts (KafkaProducer)

Environment:
  SIEM_BRIDGE_REDIS_URL       redis://redis.finguard-customer.svc.cluster.local:6379
  SIEM_BRIDGE_REDIS_PASSWORD  (from finguard redis-auth secret)
  SIEM_BRIDGE_STREAM          stream:actionable_incidents
  SIEM_BRIDGE_GROUP           omni-bridge-consumers
  SIEM_BRIDGE_CONSUMER        omni-bridge-1
  OMNI_KAFKA_BOOTSTRAP_SERVERS kafka:9092  (same as Omni workers)
  OMNI_KAFKA_TOPIC_ALERTS     omni-alerts
"""
import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from aiokafka import AIOKafkaProducer
from redis.asyncio import Redis

log = logging.getLogger("siem_bridge")
logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "message": %(message)s}',
)

# --- Config ---
REDIS_URL = os.getenv("SIEM_BRIDGE_REDIS_URL", "redis://redis.finguard-customer.svc.cluster.local:6379")
REDIS_PASSWORD = os.getenv("SIEM_BRIDGE_REDIS_PASSWORD", "")
STREAM = os.getenv("SIEM_BRIDGE_STREAM", "stream:actionable_incidents")
GROUP = os.getenv("SIEM_BRIDGE_GROUP", "omni-bridge-consumers")
CONSUMER = os.getenv("SIEM_BRIDGE_CONSUMER", "omni-bridge-1")
BLOCK_MS = int(os.getenv("SIEM_BRIDGE_BLOCK_MS", "2000"))
BATCH = int(os.getenv("SIEM_BRIDGE_BATCH", "10"))

KAFKA_SERVERS = os.getenv("OMNI_KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
KAFKA_TOPIC = os.getenv("OMNI_KAFKA_TOPIC_ALERTS", "omni-alerts")

# Dual-emit: when true, also publish raw FinGuard incident to omni-siem-raw for brain-go Kafka mode.
# Dedup note: analyst pipeline must subscribe ONLY ONE of omni-alerts or omni-siem-incidents —
# not both simultaneously — to avoid duplicate evidence for the same incident_id.
DUAL_EMIT = os.getenv("SIEM_BRIDGE_DUAL_EMIT", "false").lower() in ("true", "1", "yes")
KAFKA_TOPIC_SIEM_RAW = os.getenv("OMNI_KAFKA_TOPIC_SIEM_RAW", "omni-siem-raw")


# --- Schema translation ---
SEVERITY_MAP = {
    "critical": "critical",
    "high": "warning",
    "medium": "warning",
    "low": "info",
    "info": "info",
}

CATEGORY_TO_ALERTNAME = {
    "network_anomaly": "SIEMNetworkAnomaly",
    "auth_failure": "SIEMAuthFailure",
    "malware": "SIEMMalwareDetected",
    "ddos": "SIEMDDoSDetected",
    "data_exfil": "SIEMDataExfiltration",
    "lateral_movement": "SIEMLateralMovement",
    "k8s_threat": "SIEMKubernetesThreat",
}

# Map SIEM incident categories to pre-approved playbook IDs.
# Playbooks are stored in Redis Stack (HNSW index + semantic cache) and matched at analyst time.
# Add entries here as playbooks are approved and loaded.
CATEGORY_TO_PLAYBOOK: dict[str, str] = {
    "ddos": os.getenv("SIEM_PLAYBOOK_DDOS", ""),
    "malware": os.getenv("SIEM_PLAYBOOK_MALWARE", ""),
    "data_exfil": os.getenv("SIEM_PLAYBOOK_DATA_EXFIL", ""),
    "k8s_threat": os.getenv("SIEM_PLAYBOOK_K8S_THREAT", ""),
}

# Categories where severity=critical always requires HITL approval.
HITL_REQUIRED_CATEGORIES: frozenset[str] = frozenset(
    os.getenv("SIEM_HITL_REQUIRED_CATEGORIES", "ddos,malware,data_exfil,k8s_threat,lateral_movement").split(",")
)


def translate_incident(msg_id: str, fields: dict) -> dict:
    """
    Translate a FinGuard incident (Redis Stream message) into an Omni alert envelope
    that matches the Prometheus alertmanager → omni-gateway format.
    """
    incident_id = fields.get("id") or str(uuid.uuid4())
    severity = fields.get("severity", "medium").lower()
    category = fields.get("category", "unknown").lower()
    tenant_id = fields.get("tenant_id", "unknown")
    description = fields.get("description") or fields.get("message") or fields.get("raw_log", "")
    suggested_action = fields.get("suggested_action", "")
    affected_ip = fields.get("affected_ip", "")
    source = fields.get("source", "smart-siem")

    alert_name = CATEGORY_TO_ALERTNAME.get(category, f"SIEM{category.replace('_', '').title()}")
    omni_severity = SEVERITY_MAP.get(severity, "warning")

    # Cross-reference: preserve FinGuard incident_id in trace context
    trace_id = f"fg-{incident_id[:8]}"

    # Playbook routing: attach pre-approved playbook_id when category matches.
    playbook_id = CATEGORY_TO_PLAYBOOK.get(category, "") or ""

    # HITL gate: critical severity on designated categories always requires human approval.
    hitl_required = (
        severity == "critical" and category in HITL_REQUIRED_CATEGORIES
    ) or bool(fields.get("hitl_required"))

    alert = {
        "version": "4",
        "groupKey": f"{tenant_id}/{alert_name}",
        "status": "firing",
        "receiver": "omni-siem-bridge",
        "externalURL": "",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": alert_name,
                    "severity": omni_severity,
                    "namespace": _tenant_to_namespace(tenant_id),
                    "source": source,
                    "siem_source": "finguard",
                    "siem_tenant": tenant_id,
                    "siem_category": category,
                    "siem_incident_id": incident_id,
                    "trace_id": trace_id,
                    "siem_playbook_id": playbook_id,
                    "siem_hitl_required": "true" if hitl_required else "false",
                },
                "annotations": {
                    "description": description,
                    "suggested_action": suggested_action,
                    "affected_ip": affected_ip,
                    "siem_stream_msg_id": msg_id,
                    "bridge_ingested_at": datetime.now(timezone.utc).isoformat(),
                },
                "startsAt": fields.get("timestamp", datetime.now(timezone.utc).isoformat()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": f"smart-siem/incidents/{incident_id}",
                "fingerprint": f"{tenant_id}-{category}-{affected_ip or incident_id[:8]}",
            }
        ],
        "commonLabels": {"siem_source": "finguard"},
        "commonAnnotations": {},
        "groupLabels": {"alertname": alert_name},
    }
    return alert


def _tenant_to_namespace(tenant_id: str) -> str:
    """Map FinGuard tenant_id to a K8s namespace for Omni context."""
    # Default: route to multi-agent where Omni executors live
    # Extend this mapping if tenants map to specific namespaces
    return os.getenv("SIEM_BRIDGE_DEFAULT_NAMESPACE", "multi-agent")


# --- Main loop ---
async def ensure_group(redis: Redis) -> None:
    try:
        await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        log.info('"consumer group created" group="%s" stream="%s"', GROUP, STREAM)
    except Exception as e:
        if "BUSYGROUP" in str(e):
            log.info('"consumer group already exists" group="%s"', GROUP)
        else:
            raise


async def run(redis: Redis, producer: AIOKafkaProducer) -> None:
    await ensure_group(redis)
    log.info('"bridge started" stream="%s" group="%s" kafka_topic="%s"', STREAM, GROUP, KAFKA_TOPIC)

    while True:
        try:
            results = await redis.xreadgroup(
                groupname=GROUP,
                consumername=CONSUMER,
                streams={STREAM: ">"},
                count=BATCH,
                block=BLOCK_MS,
            )
            if not results:
                continue

            for _stream, messages in results:
                for msg_id, fields in messages:
                    await _process(redis, producer, msg_id, fields)

        except asyncio.CancelledError:
            log.info('"bridge shutting down"')
            break
        except Exception as e:
            log.error('"bridge loop error" error="%s"', e)
            await asyncio.sleep(2)


_SYNTHETIC_REASON_PREFIXES: tuple[str, ...] = (
    "autonomy_loop",
    "chaos_test",
    "synthetic_",
    "loop_test",
)


def _is_synthetic(fields: dict) -> bool:
    # Accept both flat shape (finguard native) and nested {"data": "<json>"} (tooling).
    candidates: list[dict] = [fields]
    raw = fields.get("data")
    if isinstance(raw, str):
        try:
            inner = json.loads(raw)
            if isinstance(inner, dict):
                candidates.append(inner)
        except Exception:
            pass
    for c in candidates:
        reason = str(c.get("reason") or "").lower()
        if reason.startswith(_SYNTHETIC_REASON_PREFIXES):
            return True
        tenant = str(c.get("tenant_id") or "").lower()
        if tenant.startswith(("loop-", "chaos", "synthetic-")):
            return True
        pod = str(c.get("pod") or "").lower()
        if pod.startswith(("loop-pod", "chaos-")):
            return True
    return False


async def _process(redis: Redis, producer: AIOKafkaProducer, msg_id: str, fields: dict) -> None:
    incident_id = fields.get("id", msg_id)
    if _is_synthetic(fields):
        # Ack and drop. Do not forward synthetic load-test events to Omni.
        await redis.xack(STREAM, GROUP, msg_id)
        log.info('"synthetic_dropped" incident_id="%s" reason="%s"', incident_id, fields.get("reason", ""))
        return
    try:
        alert = translate_incident(msg_id, fields)
        trace_id = alert["alerts"][0]["labels"].get("trace_id", f"fg-{msg_id[:8]}")
        inner = {"source": "siem", "trace_id": trace_id, "data": alert}
        envelope = json.dumps({"data": json.dumps(inner, ensure_ascii=False)}, ensure_ascii=False).encode()
        await producer.send_and_wait(KAFKA_TOPIC, value=envelope)

        if DUAL_EMIT:
            raw_envelope = json.dumps({
                "id": incident_id,
                "tenant_id": fields.get("tenant_id", ""),
                "severity": fields.get("severity", ""),
                "category": fields.get("category", ""),
                "source": fields.get("source", "siem-bridge"),
                "source_ip": fields.get("source_ip", ""),
                "timestamp_unix": int(fields.get("timestamp_unix", 0)),
                "schema_version": "1.0.0",
                "trace_id": trace_id,
            }, ensure_ascii=False).encode()
            await producer.send_and_wait(KAFKA_TOPIC_SIEM_RAW, value=raw_envelope)
            log.info('"dual_emit_raw" incident_id="%s" topic="%s"', incident_id, KAFKA_TOPIC_SIEM_RAW)

        await redis.xack(STREAM, GROUP, msg_id)
        log.info(
            '"incident forwarded to omni" incident_id="%s" alert="%s" severity="%s"',
            incident_id,
            alert["alerts"][0]["labels"]["alertname"],
            alert["alerts"][0]["labels"]["severity"],
        )
    except Exception as e:
        log.error('"failed to forward incident" incident_id="%s" error="%s"', incident_id, e)
        # Do not ACK — message stays pending for retry / XAUTOCLAIM


async def main() -> None:
    redis_kwargs: dict = {"decode_responses": True}
    if REDIS_PASSWORD:
        redis_kwargs["password"] = REDIS_PASSWORD

    redis = Redis.from_url(REDIS_URL, **redis_kwargs)
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA_SERVERS)

    backoff = 2
    while True:
        try:
            await producer.start()
            break
        except Exception as e:
            log.warning('"kafka_bootstrap_retry" err="%s" backoff_s=%d', e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
    try:
        await run(redis, producer)
    finally:
        await producer.stop()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
