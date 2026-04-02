"""Summary-first + max length cho tool output trước khi vào ReAct context."""

from __future__ import annotations

from typing import Any

from workers.observation_sanitize import sanitize_for_llm


def summarize_for_context(text: str, max_chars: int) -> str:
    if not text:
        return ""
    t = text.strip()
    if len(t) <= max_chars:
        return t
    return t[: max(0, max_chars - 1)] + "…"


def prepare_observation_for_llm(raw: str, max_chars: int) -> str:
    """Cắt độ dài rồi mask secrets (thứ tự: truncate trước để regex nhẹ hơn)."""
    clipped = summarize_for_context(raw, max_chars)
    return sanitize_for_llm(clipped)


def prepare_tool_return_for_llm(
    ctx: Any | None,
    raw: str,
    *,
    max_chars: int | None = None,
) -> str:
    """P0: mọi đường tool → string ngắn + mask; max từ OMNI_TOOL_OUTPUT_MAX_CHARS hoặc tham số."""
    mx = max_chars if max_chars is not None else 1500
    if ctx is not None and max_chars is None:
        ws = getattr(ctx, "settings", None)
        if ws is not None:
            mx = int(getattr(ws, "tool_output_max_chars", mx) or mx)
    return prepare_observation_for_llm(raw or "", mx)
