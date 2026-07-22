"""omni-siem-raw decode + omni-siem-incidents envelope.

Port 1-1 của brain-go ``internal/transport/kafka.go``
(``decodeKafkaMessage`` / ``incidentEnvelope``). Semantics giữ nguyên: field
sai kiểu bị coi là rỗng (không raise), message thiếu id/tenant_id bị drop.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from services.siem_correlation.entities import extract_entities
from services.siem_correlation.models import Incident

logger = logging.getLogger(__name__)


def _get_str(raw: dict[str, Any], key: str) -> str:
    v = raw.get(key)
    return v if isinstance(v, str) else ""


def _get_int(raw: dict[str, Any], key: str) -> int:
    v = raw.get(key)
    if isinstance(v, bool):  # bool is int in Python; Go would see a bool as 0
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    return 0


def _str_list(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    v = raw.get(key)
    if not isinstance(v, list):
        return ()
    return tuple(item for item in v if isinstance(item, str))


def decode_kafka_message(data: bytes | str | dict[str, Any]) -> Incident | None:
    """Parse a raw ``omni-siem-raw`` message into an Incident, or None when the
    message is undecodable / missing required fields (drop + ack semantics)."""
    if isinstance(data, dict):
        raw: Any = data
    else:
        try:
            raw = json.loads(data)
        except (ValueError, UnicodeDecodeError) as e:
            logger.warning("event=siem_corr_decode_error err=%s", e)
            return None
    if not isinstance(raw, dict):
        logger.warning("event=siem_corr_decode_error err=not_an_object")
        return None

    incident_id = _get_str(raw, "id")
    tenant_id = _get_str(raw, "tenant_id")
    if not incident_id or not tenant_id:
        logger.warning("event=siem_corr_decode_drop reason=missing_id_or_tenant")
        return None

    # Normalized message string only (already-bounded description); never a raw
    # VM log line. Used solely for allowlist entity extraction.
    body = _get_str(raw, "description") or _get_str(raw, "message") or _get_str(raw, "raw_log")

    inc = Incident(
        incident_id=incident_id,
        tenant_id=tenant_id,
        severity=_get_str(raw, "severity"),
        source=_get_str(raw, "source"),
        category=_get_str(raw, "category"),
        timestamp_unix=_get_int(raw, "timestamp_unix"),
        schema_version=_get_str(raw, "schema_version"),
        rule_id=_get_str(raw, "rule_id"),
        source_ip=_get_str(raw, "source_ip"),
        dest_ip=_get_str(raw, "dest_ip"),
        raw_log=body,
        correlation_ids=_str_list(raw, "correlation_ids"),
        tags=_str_list(raw, "tags"),
    )
    return inc.with_entities(extract_entities(inc))


def incident_envelope(inc: Incident) -> dict[str, Any]:
    """JSON envelope for ``omni-siem-incidents`` — metadata only, the message
    body never leaves through this envelope (parity: incidentEnvelope)."""
    env: dict[str, Any] = {
        "id": inc.incident_id,
        "tenant_id": inc.tenant_id,
        "severity": inc.severity,
        "source": inc.source,
        "category": inc.category,
        "timestamp_unix": inc.timestamp_unix,
        "schema_version": inc.schema_version,
    }
    if inc.source_ip:
        env["source_ip"] = inc.source_ip
    if inc.dest_ip:
        env["dest_ip"] = inc.dest_ip
    if inc.correlation_ids:
        env["correlation_ids"] = list(inc.correlation_ids)
    if inc.tags:
        env["tags"] = list(inc.tags)
    return env
