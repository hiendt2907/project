"""Evidence fingerprinting for deduplication and clustering.

A fingerprint normalizes an evidence item's content by stripping all
volatile tokens (IPs, PIDs, timestamps, pod names, numeric values) so that
two log lines carrying the same semantic error but different runtime values
produce the same fingerprint.

Fingerprint format: "{probe}:{sha256[:12]}"
Same probe + same semantic content → same fingerprint.
Different probes → always different fingerprints.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Order matters: strip more-specific patterns before generic numeric strip.
_STRIP_RULES: list[tuple[str, str, int]] = [
    # IPv4 with optional port
    (r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?\b", "IP", 0),
    # IPv6 compressed or full
    (r"\b(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{0,4}\b", "IP6", re.IGNORECASE),
    # UUIDs / hex correlation IDs
    (r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "UUID", re.IGNORECASE),
    # K8s pod names: word-RANDOM5 suffix pattern
    (r"\b([a-z][a-z0-9-]{2,})-[a-z0-9]{5,10}\b", r"\1-POD", 0),
    # PIDs
    (r"\b(?:pid|PID)\s*[=:]\s*\d+", "PID", 0),
    # Timestamps ISO8601 — lowercase after text.lower(), so [Tt] and z
    (r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:[Zz]|[+-]\d{2}:?\d{2})?", "TS", 0),
    # Unix epoch (10–13 digits)
    (r"\b\d{10,13}\b", "EPOCH", 0),
    # Metrics with unit suffix — use (?!\w) instead of \b because units like % are non-word chars
    (r"\b\d+(?:[.,]\d+)?\s*(?:ms|μs|us|ns|s|mb|gb|tb|kb|%|bytes|b/s|ops/s|rps|rpm)(?!\w)",
     "NUM", re.IGNORECASE),
    # Hex addresses (memory, pointers)
    (r"\b0x[0-9a-f]{4,}\b", "ADDR", re.IGNORECASE),
    # Remaining standalone large numbers (>= 4 digits, avoids stripping port numbers in context)
    (r"\b\d{4,}\b", "NUM", 0),
]

_COMPILED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(pat, flags), repl)
    for pat, repl, flags in _STRIP_RULES
]

# Collapse repeated whitespace/punctuation after stripping
_RE_WHITESPACE = re.compile(r"\s+")


def normalize_content(text: str) -> str:
    """Strip volatile tokens from evidence text, return normalized lowercase string.

    Lowercase first so patterns match case-insensitively; replacement tokens
    (IP, UUID, PID, TS, NUM) are uppercase to remain distinguishable from
    ordinary words after normalization.
    """
    text = text.lower()
    for pattern, replacement in _COMPILED:
        text = pattern.sub(replacement, text)
    text = _RE_WHITESPACE.sub(" ", text).strip()
    return text


def fingerprint_evidence(item: dict[str, Any]) -> str:
    """
    Compute a stable fingerprint for an evidence item.

    FAILED and PASSED results for the same probe produce different fingerprints
    so they form separate clusters and are triaged independently.

    Returns: "{probe}:{sha256_hex[:12]}"
    """
    probe = (item.get("probe") or "unknown").lower().strip()
    # Include result so FAILED clusters stay separate from PASSED clusters.
    result = (item.get("result") or "PASSED").upper()
    alert_hint = item.get("alert_hint") or ""
    raw = item.get("raw") or ""

    combined = result + " " + normalize_content(alert_hint + " " + raw)

    digest = hashlib.sha256(combined.encode("utf-8", errors="replace")).hexdigest()
    return f"{probe}:{digest[:12]}"


def fingerprint_batch(items: list[dict[str, Any]]) -> list[str]:
    """Fingerprint a list of evidence items in order."""
    return [fingerprint_evidence(item) for item in items]


def pick_representative(items: list[dict[str, Any]]) -> dict[str, Any]:
    """
    From a list of items with the same fingerprint, pick the most
    information-rich one (longest combined alert_hint + raw content).
    """
    if not items:
        raise ValueError("items must not be empty")
    return max(
        items,
        key=lambda i: len(i.get("alert_hint") or "") + len(i.get("raw") or ""),
    )
