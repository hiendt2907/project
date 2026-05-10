"""StepStateMachine — Redis-backed state tracker for playbook step execution.

Key schema:
  omni:playbook:state:{trace_id}:{playbook_id}  →  JSON
  {
    "status":      "PENDING_APPROVAL" | "APPROVED" | "EXECUTING" | "DONE" | "REJECTED" | "EXPIRED",
    "step_order":  int,
    "tool_name":   str,
    "playbook_id": str,
    "trace_id":    str,
    "updated_at":  unix epoch int,
  }

TTL: 7200 seconds (2 h). Stale entries auto-expire.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_KEY_PATTERN = "omni:playbook:state:{trace}:{playbook_id}"
_TTL_SEC = 7200

STEP_PENDING = "PENDING_APPROVAL"
STEP_APPROVED = "APPROVED"
STEP_EXECUTING = "EXECUTING"
STEP_DONE = "DONE"
STEP_REJECTED = "REJECTED"
STEP_EXPIRED = "EXPIRED"

_VALID_TRANSITIONS: dict[str, set[str]] = {
    STEP_PENDING:   {STEP_APPROVED, STEP_REJECTED, STEP_EXPIRED},
    STEP_APPROVED:  {STEP_EXECUTING},
    STEP_EXECUTING: {STEP_DONE, STEP_REJECTED},
    STEP_DONE:      set(),
    STEP_REJECTED:  set(),
    STEP_EXPIRED:   set(),
}


class StepStateMachine:
    def __init__(self, redis: Any) -> None:
        self._r = redis

    def _key(self, trace: str, playbook_id: str) -> str:
        return _KEY_PATTERN.format(trace=trace, playbook_id=playbook_id)

    async def get(self, trace: str, playbook_id: str) -> dict[str, Any] | None:
        raw = await self._r.get(self._key(trace, playbook_id))
        if not raw:
            return None
        try:
            return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            return None

    async def init(
        self,
        trace: str,
        playbook_id: str,
        step_order: int,
        tool_name: str,
    ) -> dict[str, Any]:
        state = {
            "status": STEP_PENDING,
            "step_order": step_order,
            "tool_name": tool_name,
            "playbook_id": playbook_id,
            "trace_id": trace,
            "updated_at": int(time.time()),
        }
        await self._r.setex(
            self._key(trace, playbook_id),
            _TTL_SEC,
            json.dumps(state, ensure_ascii=False),
        )
        logger.info(
            "event=playbook_step_init trace=%s playbook=%s step=%d tool=%s",
            trace, playbook_id, step_order, tool_name,
        )
        return state

    async def transition(
        self,
        trace: str,
        playbook_id: str,
        new_status: str,
    ) -> bool:
        state = await self.get(trace, playbook_id)
        if not state:
            logger.warning("event=step_state_missing trace=%s playbook=%s", trace, playbook_id)
            return False
        current = state.get("status", "")
        allowed = _VALID_TRANSITIONS.get(current, set())
        if new_status not in allowed:
            logger.warning(
                "event=step_state_invalid_transition trace=%s playbook=%s %s->%s",
                trace, playbook_id, current, new_status,
            )
            return False
        state["status"] = new_status
        state["updated_at"] = int(time.time())
        await self._r.setex(
            self._key(trace, playbook_id),
            _TTL_SEC,
            json.dumps(state, ensure_ascii=False),
        )
        logger.info(
            "event=playbook_step_transition trace=%s playbook=%s %s->%s",
            trace, playbook_id, current, new_status,
        )
        return True
