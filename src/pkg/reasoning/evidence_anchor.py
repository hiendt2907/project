"""Detect LLM output contradictions against SDK / batch evidence (heuristic)."""

from __future__ import annotations

import re
from typing import Any

# Strong SDK signals in sanitized batch text
_RE_COMPLETED = re.compile(r"(status|phase)\s*:\s*(completed|succeeded)\b", re.IGNORECASE)
_RE_FALSE_ALARM_BLOCK = re.compile(r"\bFALSE_ALARM\b|\bSTALE_METRIC\b", re.IGNORECASE)
# No trailing \\b: "CrashLoopBackOff" has no boundary after "Loop" before "BackOff".
_RE_CRASH = re.compile(r"(crashloop|crash\s*loop|backoff|oomkilled)", re.IGNORECASE)
_RE_HIGH_CPU = re.compile(r"\b(high\s+cpu|cpu\s+spike|elevated\s+cpu|usage.*high)\b", re.IGNORECASE)


def llm_contradicts_sdk_facts(llm_text: str, evidence_batch_text: str) -> bool:
    """
    Return True if LLM narrative conflicts with probe/SDK lines in the batch.
    Not exhaustive — conservative heuristics for fail-safe.
    """
    llm = (llm_text or "").strip()
    ev = (evidence_batch_text or "").strip()
    if not llm or not ev:
        return False

    if _RE_FALSE_ALARM_BLOCK.search(ev):
        if _RE_HIGH_CPU.search(llm) and not _RE_FALSE_ALARM_BLOCK.search(llm):
            return True

    if _RE_COMPLETED.search(ev) and _RE_CRASH.search(llm):
        return True

    # Completed/Succeeded workload state vs resource-spike narrative (SRE audit case).
    if _RE_COMPLETED.search(ev) and _RE_HIGH_CPU.search(llm):
        return True

    return False


def summarize_facts_for_anchor(ev_docs: list[dict[str, Any]]) -> str:
    """Compact string for contradiction checks."""
    parts: list[str] = []
    for d in ev_docs:
        parts.append(str(d.get("result") or ""))
        parts.append(str(d.get("extracted_fact") or "")[:800])
    return " ".join(parts)
