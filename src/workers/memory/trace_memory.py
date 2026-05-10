"""Redis-backed short-term memory (blackboard) for agentic planner — one trace_id scope."""

from __future__ import annotations

import json
import logging
from typing import Any, Literal
from xml.sax.saxutils import escape

from pydantic import BaseModel, Field

from workers.memory.initial_symptom import InitialSymptom

logger = logging.getLogger(__name__)

REDIS_KEY_TEMPLATE = "omni:memory:trace:{trace_id}"
TTL_SEC = 14400  # 4 hours
MAX_RESULT_SUMMARY_CHARS = 4000
MAX_RENDER_CHARS = 12000
MAX_INITIAL_SYMPTOMS_CHARS = 2000
MAX_HISTORY_RECORDS = 16


def _truncate(s: str, limit: int) -> str:
    t = (s or "").strip()
    if len(t) <= limit:
        return t
    keep = max(0, limit - 40)
    return f"{t[:keep]} [TRUNCATED orig_len={len(t)}]"


class ActionRecord(BaseModel):
    step: int
    tool_name: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    # Fingerprint of LLM-requested discovery (before pod→workload redirect); anti-loop dedupe.
    repeat_dedupe_key: str = Field(default="")
    result_summary: str = ""
    is_error: bool = False
    kind: Literal[
        "readonly_executed",
        "mutate_planned",
        "phase_done",
        "post_mutate_verify",
    ] = "readonly_executed"


class OmniTraceMemory(BaseModel):
    trace_id: str
    initial_symptoms: str = ""
    initial_symptom: InitialSymptom | None = None
    working_hypothesis: str = "Collecting initial diagnostic data..."
    action_history: list[ActionRecord] = Field(default_factory=list)
    attempt_count: int = 0

    def render_llm_context(self) -> str:
        """Markdown for <HISTORY> — token-bounded."""
        lines: list[str] = []
        sym = _truncate(self.initial_symptoms, MAX_INITIAL_SYMPTOMS_CHARS)
        if sym:
            lines.append(f"- Initial symptoms (truncated): {sym}")
        hist = self.action_history[-MAX_HISTORY_RECORDS:]
        if not hist:
            lines.append("- No prior read-only or plan actions in this trace yet.")
        else:
            lines.append("- Prior actions (most recent last):")
            for rec in hist:
                args_s = json.dumps(rec.args, sort_keys=True, default=str) if rec.args else "{}"
                if len(args_s) > 400:
                    args_s = args_s[:399] + "…"
                err = " ERROR" if rec.is_error else ""
                summ = _truncate(rec.result_summary, MAX_RESULT_SUMMARY_CHARS)
                lines.append(
                    f"  - step={rec.step} kind={rec.kind} tool={rec.tool_name!r}{err} "
                    f"args={args_s} → {summ}"
                )
        out = "\n".join(lines)
        return _truncate(out, MAX_RENDER_CHARS)


def format_initial_symptom_block(mem: OmniTraceMemory) -> str:
    """XML fragment for structured Prometheus alert identity (empty if unset)."""
    if mem.initial_symptom is None:
        return ""
    raw = mem.initial_symptom.render_for_prompt()
    return f"  <INITIAL_SYMPTOM>\n{escape(raw)}\n  </INITIAL_SYMPTOM>\n"


def format_trace_memory_block(mem: OmniTraceMemory) -> str:
    """XML wrapper for prompt injection; text nodes escaped for safety."""
    ini = format_initial_symptom_block(mem)
    hyp = escape(mem.working_hypothesis or "")
    hist_raw = mem.render_llm_context()
    hist = escape(hist_raw)
    return (
        "<TRACE_MEMORY>\n"
        f"{ini}"
        f"  <HYPOTHESIS>{hyp}</HYPOTHESIS>\n"
        "  <HISTORY>\n"
        f"{hist}\n"
        "  </HISTORY>\n"
        "</TRACE_MEMORY>"
    )


def _redis_key(trace_id: str) -> str:
    return REDIS_KEY_TEMPLATE.format(trace_id=trace_id)


async def load_trace_memory(
    redis: Any,
    trace_id: str,
    *,
    initial_symptoms: str,
    initial_symptom: InitialSymptom | None = None,
    seed_attempt: int = 0,
) -> OmniTraceMemory:
    """Load from Redis or create fresh. ``redis`` may be None → in-memory only (no persistence)."""
    if not trace_id:
        trace_id = "unknown"
    sym = _truncate(initial_symptoms, MAX_INITIAL_SYMPTOMS_CHARS)
    fresh = OmniTraceMemory(
        trace_id=trace_id,
        initial_symptoms=sym,
        initial_symptom=initial_symptom,
        attempt_count=max(0, int(seed_attempt)),
    )
    if redis is None:
        return fresh
    try:
        raw = await redis.get(_redis_key(trace_id))
        if not raw:
            return fresh
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return fresh
        # Refresh initial_symptoms if caller has richer text than stored empty
        mem = OmniTraceMemory.model_validate(data)
        if sym and (not (mem.initial_symptoms or "").strip()):
            mem.initial_symptoms = sym
        if initial_symptom is not None and mem.initial_symptom is None:
            mem.initial_symptom = initial_symptom
        if seed_attempt and mem.attempt_count < seed_attempt:
            mem.attempt_count = seed_attempt
        return mem
    except Exception as e:
        logger.warning("load_trace_memory failed trace=%s: %s", trace_id, e)
        return fresh


async def save_trace_memory(redis: Any, mem: OmniTraceMemory) -> None:
    if redis is None:
        return
    try:
        payload = mem.model_dump(mode="json")
        await redis.setex(_redis_key(mem.trace_id), TTL_SEC, json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        logger.warning("save_trace_memory failed trace=%s: %s", mem.trace_id, e)


async def append_post_mutate_verify_record(
    redis: Any,
    trace_id: str,
    *,
    verify_summary: str,
    initial_symptoms: str = "",
    initial_symptom: InitialSymptom | None = None,
) -> None:
    """Audit: record fresh post-mutate re-probe summary before planner state gate."""
    if not trace_id:
        return
    mem = await load_trace_memory(
        redis,
        trace_id,
        initial_symptoms=initial_symptoms,
        initial_symptom=initial_symptom,
    )
    nxt = len(mem.action_history) + 1
    mem.action_history.append(
        ActionRecord(
            step=nxt,
            tool_name="post_mutate_reprobe",
            args={},
            result_summary=truncate_for_action_record(verify_summary),
            is_error=False,
            kind="post_mutate_verify",
        )
    )
    mem.attempt_count = len(mem.action_history)
    await save_trace_memory(redis, mem)


def truncate_for_action_record(text: str, *, max_chars: int | None = None) -> str:
    cap = max_chars if max_chars is not None else MAX_RESULT_SUMMARY_CHARS
    return _truncate(text, cap)
