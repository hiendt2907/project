"""Configurable truncation for LLM-bound strings (prompt, memory windows)."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


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
