"""HITL Fallback Channel — Slack webhook sender (S1.3).

Called when an HITL approval has been pending beyond the escalation threshold
without a decision, to alert an additional channel beyond Telegram.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("hitl_fallback")

_SLACK_TIMEOUT_SEC = 8.0


async def emit_slack_fallback(
    client: Any,  # httpx.AsyncClient — typed loosely to avoid import at module level
    webhook_url: str,
    trace_id: str,
    tool_name: str,
    incident_id: str,
    explain: str,
    elapsed_sec: int,
) -> bool:
    """POST a Slack Block Kit message to `webhook_url`.

    Returns True on success, False on any failure (non-fatal — caller must not
    raise on False).
    """
    if not webhook_url:
        return False

    text = (
        f":rotating_light: *HITL Escalation* — no decision after {elapsed_sec}s\n"
        f"*Trace:* `{trace_id}`  *Tool:* `{tool_name}`  *Incident:* `{incident_id}`"
    )
    if explain:
        text += f"\n*Explain:* {explain[:200]}"

    payload = {
        "text": text,
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Omni HITL Dispatcher | trace=`{trace_id}`",
                    }
                ],
            },
        ],
    }

    try:
        resp = await client.post(
            webhook_url,
            content=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
            timeout=_SLACK_TIMEOUT_SEC,
        )
        if resp.status_code == 200:
            log.info(
                '"hitl_fallback_slack_sent" trace="%s" elapsed_sec=%d',
                trace_id, elapsed_sec,
            )
            return True
        log.warning(
            '"hitl_fallback_slack_failed" trace="%s" status=%d body="%s"',
            trace_id, resp.status_code, resp.text[:200],
        )
        return False
    except Exception as exc:
        log.warning(
            '"hitl_fallback_slack_exception" trace="%s" err="%s"',
            trace_id, exc,
        )
        return False


async def store_dead_letter(
    redis: Any,
    trace_id: str,
    incident_id: str,
    tool_name: str,
    raw_body: dict,
    reason: str,
    ttl: int = 86400,
) -> None:
    """Store a timed-out HITL action in Redis dead-letter hash.

    Key: omni:hitl:deadletter:{incident_id}
    Admins can replay via: kubectl exec → redis-cli hgetall omni:hitl:deadletter:{incident_id}
    """
    if redis is None:
        return
    key = f"omni:hitl:deadletter:{incident_id}"
    try:
        import time as _time
        await redis.hset(key, mapping={
            "trace_id": trace_id,
            "tool_name": tool_name,
            "action_json": json.dumps(raw_body, ensure_ascii=False),
            "expired_at": str(_time.time()),
            "reason": reason,
        })
        await redis.expire(key, ttl)
        log.info(
            '"hitl_dead_letter_stored" trace="%s" incident_id="%s" key="%s"',
            trace_id, incident_id, key,
        )
    except Exception as exc:
        log.warning(
            '"hitl_dead_letter_store_failed" trace="%s" err="%s"',
            trace_id, exc,
        )
