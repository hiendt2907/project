"""Opt-in structured INFO logs for LLM raw output and JSON/tool parse outcomes (Ollama / OpenAI-compatible)."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _cap(s: str, max_chars: int) -> str:
    t = (s or "").strip()
    if len(t) <= max_chars:
        return t
    keep = max(0, max_chars - 40)
    return f"{t[:keep]} [TRUNCATED orig_len={len(t)}]"


def llm_trace_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "omni_llm_trace_enabled", False))


def agentic_parse_failure_hint(raw: str) -> str:
    """Why _parse_agentic_json might return None (for diagnostics only)."""
    s = (raw or "").strip()
    if not s:
        return "empty_content"
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return "no_balanced_json_braces"
    try:
        o = json.loads(s[i : j + 1])
        if isinstance(o, dict):
            return "parse_would_succeed"
        return "json_not_object"
    except Exception as e:
        return f"json_decode:{type(e).__name__}"


def log_llm_trace(
    settings: Any,
    *,
    trace: str,
    phase: str,
    model: str | None = None,
    step: int | None = None,
    raw_response: str | None = None,
    parse_ok: bool | None = None,
    parse_hint: str | None = None,
    parsed_tool: str | None = None,
    reject_reason: str | None = None,
    detail: str | None = None,
) -> None:
    if not llm_trace_enabled(settings):
        return
    parts: list[str] = [
        "event=llm_trace",
        f"phase={phase}",
        f"trace={trace}",
    ]
    if model:
        parts.append(f"model={model}")
    if step is not None:
        parts.append(f"step={step}")
    if parse_ok is not None:
        parts.append(f"parse_ok={parse_ok}")
    if parse_hint:
        parts.append(f"parse_hint={parse_hint}")
    if parsed_tool:
        parts.append(f"parsed_tool={parsed_tool}")
    if reject_reason:
        parts.append(f"reject_reason={reject_reason}")
    if detail:
        parts.append(f"detail={_cap(detail, 2000)}")
    line = " ".join(parts)
    if raw_response is not None:
        line += f" raw_len={len(raw_response)} raw={_cap(raw_response, 4500)}"
    logger.info(line)
