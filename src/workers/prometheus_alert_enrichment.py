"""Infer alert trigger dimension + grounded English hints for Ollama (reduce hallucination)."""

from __future__ import annotations

import json
import re
from typing import Any


def infer_alert_trigger_dimension(
    labels: dict[str, Any],
    annots: dict[str, Any],
    alertname: str,
    summary: str,
) -> str:
    """Classify incident focus from labels/annotations/name — pattern-based, no fixed alert names."""
    parts = [
        alertname or "",
        summary or "",
        str(annots.get("summary") or ""),
        str(annots.get("description") or ""),
        str(annots.get("message") or ""),
        json.dumps(labels, sort_keys=True),
    ]
    blob = " ".join(parts).lower()
    checks: list[tuple[re.Pattern[str], str]] = [
        (re.compile(r"\b(readiness|liveness|startup)\s*probe|probe\s*(fail|error|warning)|/health|healthcheck"), "probe"),
        (re.compile(r"\bcpu\b|throttl|cputhrottl|load\s*avg"), "cpu"),
        (re.compile(r"\bmemory\b|\boom\b|\bram\b|mem\s*limit|evict"), "memory"),
        (re.compile(r"\bdisk\b|volume|pvc\b|pv\b|filesystem|inode|ephemeral"), "disk"),
        (re.compile(r"\bnetwork\b|latency|timeout|refused|reset|dns"), "network"),
        (re.compile(r"\bunhealthy\b|not\s*ready|crashloop|back\s*off|down\b"), "availability"),
    ]
    for rx, dim in checks:
        if rx.search(blob):
            return dim
    return "unknown"


def build_ollama_anchor_en(
    *,
    namespace: str,
    pod: str,
    deployment: str,
    trigger: str,
) -> str:
    """English-only anchor block: FACTS + TRIGGER + HINT (grounded; ignore unrelated RAG context)."""
    facts: list[str] = []
    if namespace.strip():
        facts.append(f"namespace={namespace.strip()}")
    if pod.strip():
        facts.append(f"pod={pod.strip()}")
    if deployment.strip():
        facts.append(f"deployment={deployment.strip()}")
    facts_line = ", ".join(facts) if facts else "identifiers=unspecified"
    if facts:
        hint = (
            "Use ONLY identifiers listed under FACTS for this alert. "
            "Ignore pod names from unrelated [CONTEXT] / topology blurbs. "
            f"Investigate TRIGGER={trigger} on that workload first (inspect/describe before list/top)."
        )
    else:
        hint = (
            "No pod/deployment/namespace in the alert — **cluster discovery is mandatory before concluding**. "
            "First call one of: `list_all_pods_sdk` (args e.g. limit=200), `promql_instant`, or `query_prometheus_metrics`. "
            "`identifiers=unspecified` is NOT permission to escalate without at least one observation tool. "
            "Ignore unrelated pod names in [CONTEXT] topology unless they match the alert rule."
        )
    return (
        "[OLLAMA_ANCHOR_EN]\n"
        f"FACTS: {facts_line}\n"
        f"TRIGGER: {trigger}\n"
        f"HINT: {hint}"
    )
