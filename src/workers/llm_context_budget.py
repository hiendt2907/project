"""Configurable truncation and LLM option helpers for Omni workers."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def llm_num_ctx(ctx_or_settings: Any, default: int = 8192) -> int:
    """Read llm_num_ctx from a WorkerSettings or a context object that wraps one."""
    ws = getattr(ctx_or_settings, "settings", ctx_or_settings)
    raw = getattr(ws, "llm_num_ctx", None)
    return int(raw) if isinstance(raw, (int, float)) else default


def build_llm_options(
    ctx_or_settings: Any,
    *,
    temperature: float = 0.1,
    think: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a consistent Ollama options dict from WorkerSettings / context.

    num_ctx is read from settings.llm_num_ctx (default 8192).
    Callers that need num_predict should set it in *extra*.
    """
    opts: dict[str, Any] = {
        "temperature": temperature,
        "num_ctx": llm_num_ctx(ctx_or_settings),
    }
    if think is not None:
        opts["think"] = think
    if extra:
        opts.update(extra)
    return opts


def effective_reply_max_words(ws: Any) -> int:
    """Concise cap (default 30) bounded by omni_summary_max_words."""
    sm = int(getattr(ws, "omni_summary_max_words", 100) or 100)
    c = getattr(ws, "omni_concise_reply_max_words", None)
    if c is None:
        return sm
    return min(max(10, int(c)), sm)


def truncate_to_words(text: str, max_words: int) -> str:
    """Whitespace-split cap for Telegram / concise replies."""
    if max_words <= 0:
        return ""
    words = (text or "").split()
    if len(words) <= max_words:
        return (text or "").strip()
    return " ".join(words[:max_words]).strip()


def truncate_for_llm(text: str, max_chars: int, *, tail: bool = True) -> str:
    """Truncate to at most ``max_chars`` UTF-8 codepoints; prefer tail when ``tail`` is True."""
    if max_chars <= 0:
        return ""
    t = text or ""
    if len(t) <= max_chars:
        return t
    logger.debug("truncate_for_llm: clipped len=%s max_chars=%s tail=%s", len(t), max_chars, tail)
    if tail:
        return t[-max_chars:]
    return t[:max_chars]
