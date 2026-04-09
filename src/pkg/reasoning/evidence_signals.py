"""Shared deterministic signals extracted from diagnostic evidence batches (no LLM)."""

from __future__ import annotations

import json
import re
from typing import Any

_RE_CRITICAL_FAULT = re.compile(
    r"(crashloop|createcontainer|imagepull|oomkilled|oom|failedmount|unschedul|readiness.*fail|liveness.*fail|waiting|backoff|exit[_\s-]*code)",
    re.IGNORECASE,
)


def critical_evidence_present(batch: list[dict[str, Any]]) -> bool:
    """True when alert hint / labels suggest a hard workload fault (proof-of-fault lane input)."""
    for b in batch:
        hint = str(b.get("alert_hint") or "")
        if _RE_CRITICAL_FAULT.search(hint):
            return True
        snip = str(b.get("canonical_query_snippet") or "").strip()
        if not snip.startswith("{"):
            continue
        try:
            j = json.loads(snip)
        except Exception:
            continue
        if not isinstance(j, dict):
            continue
        labels = j.get("labels")
        if not isinstance(labels, dict):
            continue
        reason = str(labels.get("reason") or "")
        alertname = str(labels.get("alertname") or "")
        if _RE_CRITICAL_FAULT.search(reason) or _RE_CRITICAL_FAULT.search(alertname):
            return True
    return False
