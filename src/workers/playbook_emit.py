"""Emit EXECUTE_PLAYBOOK lên omni-actions (CRAT fail-closed trước khi gửi).

Dual-run an toàn: caller (autonomous_decider._dispatch_tool_call) chỉ gọi khi
``OMNI_PLAYBOOK_FIRST=true`` VÀ matcher tìm được PlaybookSpec khớp fault; mọi
trường hợp khác giữ nguyên đường EXECUTE_MUTATE cũ.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from services.audit_ledger.chain_writer import write_audit_block
from services.audit_ledger.crat_event_types import CRAT_EVENT_MUTATION_ENQUEUED
from services.audit_ledger.signer import AuditLedgerError

logger = logging.getLogger(__name__)


async def emit_execute_playbook(
    ctx: Any,
    *,
    trace: str,
    playbook_id: str,
    render_ctx: dict[str, str],
    tenant: str = "default",
    hitl_approved: bool = False,
    rationale: str = "",
) -> bool:
    k = getattr(ctx, "kafka", None)
    ws = getattr(ctx, "settings", None)
    r = getattr(ctx, "redis", None)
    if k is None or ws is None or r is None:
        logger.warning("event=execute_playbook_emit_aborted trace=%s reason=missing_ctx", trace)
        return False
    body = {
        "action": "EXECUTE_PLAYBOOK",
        "trace_id": trace,
        "data": {
            "playbook_id": playbook_id,
            "render_ctx": {k_: str(v) for k_, v in (render_ctx or {}).items()},
            "tenant": tenant,
            "hitl_approved": bool(hitl_approved),
            "rationale": (rationale or "")[:500],
        },
    }
    try:
        await write_audit_block(
            event_type=CRAT_EVENT_MUTATION_ENQUEUED,
            trace_id=trace,
            payload={
                "trace_id": trace,
                "kind": "EXECUTE_PLAYBOOK",
                "playbook_id": playbook_id,
                "tenant": tenant,
                "render_ctx": body["data"]["render_ctx"],
            },
            redis=r,
            kafka=k,
            kafka_topic=getattr(ws, "kafka_topic_audit_chain", "omni-audit-chain"),
        )
    except AuditLedgerError as exc:
        logger.critical(
            "event=playbook_enqueue_audit_failed trace=%s playbook=%s err=%s FAIL_CLOSED",
            trace, playbook_id, exc,
        )
        return False
    try:
        await k.send_dict(ws.kafka_topic_actions, {"data": json.dumps(body, ensure_ascii=False)})
    except Exception as exc:  # noqa: BLE001
        logger.critical("event=playbook_kafka_send_failed trace=%s err=%s", trace, exc)
        return False
    logger.info(
        "event=action_emitted action=EXECUTE_PLAYBOOK trace=%s playbook=%s tenant=%s",
        trace, playbook_id, tenant,
    )
    return True
