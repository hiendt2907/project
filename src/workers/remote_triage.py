"""Remote agent evidence triage — Stage 3 of the SRE cognitive pipeline.

For each LogCluster:
1. RAG lookup (recall_playbook_advisory) — "check memory first"
2. If known pattern → KNOWN_WITH_FIX or KNOWN_BASELINE
3. If unknown → assess urgency → UNKNOWN_RESEARCH or UNKNOWN_ARCHIVE_ONLY

Routes:
  KNOWN_WITH_FIX       — RAG hit (score >= threshold), has actionable tool
  KNOWN_BASELINE       — RAG hit, advisory_only (no mutation needed)
  UNKNOWN_RESEARCH     — RAG miss, urgency critical/high/medium → queue for LLM
  UNKNOWN_ARCHIVE_ONLY — RAG miss, urgency baseline → archive only
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from pkg.reasoning.domain_signals import assess_domain_severity
from pkg.reasoning.evidence_cluster import LogCluster
from workers.archivist import RecallResult, recall_playbook_advisory

logger = logging.getLogger(__name__)

TriageRoute = Literal[
    "KNOWN_WITH_FIX",
    "KNOWN_BASELINE",
    "UNKNOWN_RESEARCH",
    "UNKNOWN_ARCHIVE_ONLY",
]

_RECALL_TRIAGE_THRESHOLD = 0.75
_BASELINE_URGENCIES = frozenset({"baseline", "none"})


@dataclass
class TriageResult:
    route: TriageRoute
    cluster: LogCluster
    urgency: str = "baseline"
    recall: RecallResult | None = None


def _build_symptom_text(cluster: LogCluster) -> str:
    """Construct RAG query text from cluster representative and metadata."""
    rep = cluster.representative
    parts = [
        f"domain={cluster.domain}",
        f"probe={cluster.probe}",
        f"lane={cluster.lane}",
    ]
    alert_hint = (rep.get("alert_hint") or "").strip()
    raw = (rep.get("raw") or "").strip()
    if alert_hint:
        parts.append(f"alert: {alert_hint}")
    if raw:
        parts.append(f"raw: {raw[:300]}")
    failed = cluster.results.get("FAILED", 0)
    if failed:
        parts.append(f"failed_count={failed}")
    return " ".join(parts)


def _assess_urgency(cluster: LogCluster) -> str:
    """Assess how urgently this cluster needs LLM research.

    Combines domain severity signal with FAILED ratio to produce
    critical/high/medium/baseline urgency rating.
    """
    rep = cluster.representative
    extracted = rep.get("extracted_fact")
    # extracted_fact may arrive as a JSON string (serialized through the evidence
    # envelope) — parse it so severity Priority-1 (result==FAILED) is not silently
    # skipped, which would under-rate urgency for real remote agents too.
    if isinstance(extracted, str):
        try:
            extracted = json.loads(extracted)
        except (ValueError, TypeError):
            extracted = {}
    fact: dict[str, Any] = extracted if isinstance(extracted, dict) else {}

    domain_severity = assess_domain_severity(
        cluster.domain,
        rep.get("alert_hint") or "",
        rep.get("raw") or "",
        fact,
    )

    failed_ratio = cluster.results.get("FAILED", 0) / max(cluster.count, 1)

    if domain_severity == "critical" or (domain_severity == "high" and failed_ratio > 0.5):
        return "critical"
    if domain_severity == "high" or (domain_severity == "medium" and failed_ratio > 0.3):
        return "high"
    if domain_severity == "medium" or failed_ratio > 0.2:
        return "medium"
    return "baseline"


async def triage_cluster(ctx: Any, cluster: LogCluster) -> TriageResult:
    """Route a LogCluster to the appropriate processing path.

    RAG memory is checked first — LLM is only called on a cache miss.
    """
    symptom_text = _build_symptom_text(cluster)

    recall = await recall_playbook_advisory(
        ctx,
        query_text=symptom_text,
        trace=cluster.fingerprint,
    )

    if recall is not None and recall.top_score >= _RECALL_TRIAGE_THRESHOLD:
        route: TriageRoute = (
            "KNOWN_WITH_FIX"
            if recall.top_tool and recall.top_tool != "advisory_only"
            else "KNOWN_BASELINE"
        )
        urgency = _assess_urgency(cluster)
        logger.info(
            "[TRIAGE] fp=%s route=%s rag_score=%.3f top_tool=%s",
            cluster.fingerprint, route, recall.top_score, recall.top_tool,
        )
        return TriageResult(route=route, cluster=cluster, urgency=urgency, recall=recall)

    urgency = _assess_urgency(cluster)
    route = "UNKNOWN_RESEARCH" if urgency not in _BASELINE_URGENCIES else "UNKNOWN_ARCHIVE_ONLY"

    logger.info(
        "[TRIAGE] fp=%s route=%s urgency=%s rag_miss=True",
        cluster.fingerprint, route, urgency,
    )
    return TriageResult(route=route, cluster=cluster, urgency=urgency, recall=None)
