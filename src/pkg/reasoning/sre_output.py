"""Post-LLM compaction: Symptom → Cause → Action, hard word cap, strip fluff."""

from __future__ import annotations

import re

from pkg.reasoning import llm_prompts_en as ope

# Greetings / filler lines (English) often emitted by chatty models.
_FLUFF_LINE = re.compile(
    r"^\s*(hi|hello|hey|thanks|thank you|here'?s|below|based on|in summary|as an ai)[,:]?\s*.*$",
    re.IGNORECASE | re.MULTILINE,
)
_BULLET_NOISE = re.compile(r"^\s*[-*•]\s*(here|below|note that)\b.*$", re.IGNORECASE | re.MULTILINE)


def strip_sre_fluff(text: str) -> str:
    """Remove common greetings and meta lines; keep substantive content."""
    if not (text or "").strip():
        return ""
    s = (text or "").strip()
    lines = []
    for line in s.splitlines():
        if _FLUFF_LINE.match(line.strip()):
            continue
        if _BULLET_NOISE.match(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def compact_sre_diagnosis(text: str, *, max_words: int = 100) -> str:
    """
    Hard-cap words; prefer Symptom / Cause / Action structure when present.
    Applied after LLM, before Telegram / action correlation.
    """
    t = strip_sre_fluff(text)
    if not t:
        return ""
    return ope.truncate_plain_text_to_max_words(t, max_words=max(1, int(max_words)))
