"""Mask sensitive substrings trước khi đưa Observation vào LLM context."""

from __future__ import annotations

import re

_REDACT = "[REDACTED]"

# password=secret, api_key: xxx, Bearer jwt...
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)authorization\s*:\s*bearer\s+[^\s\n]+"), f"Authorization: Bearer {_REDACT}"),
    (
        re.compile(r"(?i)authorization\s*[:=]\s*(?!bearer\s+\[REDACTED\])[^\s\n]+"),
        f"Authorization: {_REDACT}",
    ),
    (re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*[^\s\n]+"), _REDACT),
    (re.compile(r"(?i)bearer\s+[^\s\n]+"), f"Bearer {_REDACT}"),
    (re.compile(r"(?i)(-----BEGIN [A-Z ]+-----)[\s\S]*?(-----END [A-Z ]+-----)"), _REDACT),
]


def sanitize_for_llm(text: str) -> str:
    """Deterministic mask — không dựa LLM."""
    if not text:
        return text
    s = text
    for pat, repl in _PATTERNS:
        s = pat.sub(repl, s)
    return s
