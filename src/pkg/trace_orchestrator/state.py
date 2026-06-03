"""Redis-backed state for trace orchestration (RAG trials → LLM → verify)."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

REDIS_KEY_TRACE_ORCHESTRATOR_PREFIX = "omni:trace_orchestrator:"

_DEFAULT_TTL_SEC = 86_400


class TraceOrchestratorPhase(StrEnum):
    COLLECT = "collect"
    RAG_TRIALS = "rag_trials"
    LLM_TOOLS = "llm_tools"
    VERIFY = "verify"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


@dataclass
class TraceOrchestratorState:
    trace_id: str
    phase: TraceOrchestratorPhase = TraceOrchestratorPhase.RAG_TRIALS
    rag_candidate_ids: list[str] = field(default_factory=list)
    attempted_rag_ids: list[str] = field(default_factory=list)
    last_verify_ok: bool | None = None
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["phase"] = self.phase.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TraceOrchestratorState:
        phase_raw = data.get("phase") or TraceOrchestratorPhase.RAG_TRIALS.value
        try:
            phase = TraceOrchestratorPhase(str(phase_raw))
        except ValueError:
            phase = TraceOrchestratorPhase.RAG_TRIALS
        return cls(
            trace_id=str(data.get("trace_id") or ""),
            phase=phase,
            rag_candidate_ids=[str(x) for x in (data.get("rag_candidate_ids") or [])],
            attempted_rag_ids=[str(x) for x in (data.get("attempted_rag_ids") or [])],
            last_verify_ok=data.get("last_verify_ok"),
            last_error=str(data.get("last_error") or ""),
        )


def redis_key_trace_orchestrator(trace_id: str) -> str:
    return f"{REDIS_KEY_TRACE_ORCHESTRATOR_PREFIX}{trace_id}"


async def load_trace_orchestrator_state(redis: Any, trace_id: str) -> TraceOrchestratorState | None:
    key = redis_key_trace_orchestrator(trace_id)
    try:
        raw = await redis.get(key)
    except Exception as e:
        logger.debug("trace_orchestrator load skip trace=%s err=%s", trace_id, e)
        return None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return TraceOrchestratorState.from_dict(data)
    except Exception as e:
        logger.warning("trace_orchestrator corrupt trace=%s err=%s", trace_id, e)
        return None


async def save_trace_orchestrator_state(
    redis: Any,
    state: TraceOrchestratorState,
    *,
    ttl_sec: int = _DEFAULT_TTL_SEC,
) -> bool:
    key = redis_key_trace_orchestrator(state.trace_id)
    payload = json.dumps(state.to_dict(), ensure_ascii=False)
    try:
        await redis.setex(key, max(60, int(ttl_sec)), payload)
        return True
    except Exception as e:
        logger.warning("trace_orchestrator save failed trace=%s err=%s", state.trace_id, e)
        return False


async def mark_trace_orchestrator_resolved_verified(redis: Any, trace_id: str) -> bool:
    """Persist RESOLVED phase after SDK + state-machine verify terminal success."""
    tid = str(trace_id or "").strip()
    if not tid:
        return False
    st = await load_trace_orchestrator_state(redis, tid)
    if st is None:
        return True
    st.phase = TraceOrchestratorPhase.RESOLVED
    st.last_verify_ok = True
    st.last_error = ""
    return await save_trace_orchestrator_state(redis, st)
