"""Redis session_state:{chat_id} — trí nhớ ngắn hạn (goal, pending, summary, turn_count, recent)."""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

SESSION_KEY_PREFIX = "session_state:"

# Gom pod/namespace/intent (tương đương flow AWAIT_MONITORING_TARGET / slot-filling) — đủ bộ mới gọi query_victoria_metrics
PENDING_AWAIT_VM_SLOTS = "await_vm_slots"

SHORT_CLARIFICATION_QUESTION_VI = "Đại ca muốn check của Host hay Pod nào?"


class SessionState(BaseModel):
    """Schema Redis session_state:{chat_id}."""

    last_goal: str = ""
    pending_action: str = ""
    accumulated_vm_slots: dict[str, Any] = Field(default_factory=dict)
    # host | pod | '' — follow-up chart/VM kế thừa
    monitoring_target_type: str = ""
    # Sau list_all_pods_sdk — gợi ý resolve namespace
    last_pod_discovery: list[dict[str, str]] = Field(default_factory=list)
    last_summary: str = ""
    turn_count: int = 0
    recent_messages: list[dict[str, str]] = Field(default_factory=list)


def redis_key_session(chat_id: int) -> str:
    return f"{SESSION_KEY_PREFIX}{chat_id}"


async def load_session(r: Any, chat_id: int) -> SessionState:
    raw = await r.get(redis_key_session(chat_id))
    if not raw:
        return SessionState()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return SessionState.model_validate(data)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("session_state parse fail chat_id=%s: %s", chat_id, e)
    return SessionState()


async def save_session(r: Any, chat_id: int, state: SessionState, *, ttl_sec: int) -> None:
    await r.set(
        redis_key_session(chat_id),
        state.model_dump_json(),
        ex=ttl_sec,
    )


async def delete_session(r: Any, chat_id: int) -> None:
    await r.delete(redis_key_session(chat_id))
