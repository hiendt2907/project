"""Configurable truncation for LLM-bound strings (prompt, memory windows)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


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
