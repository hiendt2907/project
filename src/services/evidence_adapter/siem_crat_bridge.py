"""siem_crat_bridge — write SIEM remediation events to the CRAT audit chain.

CRAT is fail-closed: write_audit_block raises AuditLedgerError on any failure.
Callers MUST NOT proceed with downstream actions if this function raises.
"""

from __future__ import annotations

import logging
from typing import Any

from services.audit_ledger.chain_writer import write_audit_block

logger = logging.getLogger(__name__)

_KAFKA_AUDIT_TOPIC = "omni-audit-chain"


async def write_siem_remediation_to_crat(
    incident_id: str,
    category: str,
    action_taken: str,
    outcome: str,
    ctx: Any,
) -> dict[str, Any]:
    """Write a SIEM_REMEDIATION block to the CRAT audit chain.

    This is fail-closed: AuditLedgerError is raised on any failure.
    Callers must abort their transaction if this raises.

    Args:
        incident_id: FinGuard incident ID used as trace_id.
        category: SIEM incident category (e.g. "ddos", "malware").
        action_taken: Description of the remediation action.
        outcome: Result of the action (e.g. "success", "rejected", "fail").
        ctx: Worker context with .redis and .kafka attributes.

    Returns:
        The written CRAT block dict.

    Raises:
        AuditLedgerError: If Redis or Kafka write fails (fail-closed).
    """
    block = await write_audit_block(
        event_type="SIEM_REMEDIATION",
        trace_id=incident_id,
        payload={
            "category": category,
            "action_taken": action_taken,
            "outcome": outcome,
        },
        redis=ctx.redis,
        kafka=ctx.kafka,
        kafka_topic=_KAFKA_AUDIT_TOPIC,
    )
    logger.info(
        "event=siem_remediation_crat_written incident_id=%s category=%s outcome=%s seq=%d",
        incident_id,
        category,
        outcome,
        block.get("seq", -1),
    )
    return block
