"""Shared deterministic signals extracted from diagnostic evidence batches (no LLM)."""

from __future__ import annotations

import json
import re
from typing import Any

from pkg.reasoning.deterministic_mutate_from_evidence import (
    env_default_remediation_namespace,
    parse_probe_driven_mutate_tools_csv,
    probe_structured_remediation_ready,
)

_RE_CRITICAL_FAULT = re.compile(
    r"(crashloop|createcontainer|imagepull|oomkilled|oom|failedmount|unschedul|readiness.*fail|liveness.*fail|waiting|backoff|exit[_\s-]*code)",
    re.IGNORECASE,
)


def _extracted_fact_dict(item: dict[str, Any]) -> dict[str, Any] | None:
    raw = item.get("extracted_fact")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            o = json.loads(raw)
            return o if isinstance(o, dict) else None
        except Exception:
            return None
    return None


def critical_evidence_present(batch: list[dict[str, Any]]) -> bool:
    """True when alert hint / labels suggest a hard workload fault (proof-of-fault lane input)."""
    if probe_structured_remediation_ready(
        batch,
        default_ns=env_default_remediation_namespace(),
        allowed_tools=parse_probe_driven_mutate_tools_csv(""),
    ):
        return True
    for b in batch:
        hint = str(b.get("alert_hint") or "")
        if _RE_CRITICAL_FAULT.search(hint):
            return True
        # Probe narrative + structured_hint (SDK clinical) — often carries CrashLoop/OOM
        # while canonical_query_snippet is truncated or missing labels.
        raw_blob = f"{b.get('raw') or ''} {b.get('result') or ''}"
        if _RE_CRITICAL_FAULT.search(raw_blob):
            return True
        ef = _extracted_fact_dict(b)
        if isinstance(ef, dict):
            if ef.get("has_crash_loop") is True or ef.get("has_oom_killed") is True:
                return True
            if str(ef.get("phase") or "").strip() and str(ef.get("phase")).lower() not in (
                "running",
                "succeeded",
            ):
                if ef.get("ready_false") is True:
                    return True
            for pod in ef.get("pods") or []:
                if isinstance(pod, dict) and (
                    pod.get("has_crash_loop") is True or pod.get("has_oom_killed") is True
                ):
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
