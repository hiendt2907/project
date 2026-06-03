"""Remote agent LLM analysis — Stage 4 of the SRE cognitive pipeline.

Called only for UNKNOWN_RESEARCH clusters (RAG miss + urgency critical/high/medium).
Builds evidence text from LogCluster and calls run_advisory_analyst() for diagnosis.
"""
from __future__ import annotations

import logging
from typing import Any

from pkg.reasoning.analyst_advisory_schema import AnalystAdvisory
from pkg.reasoning.domain_signals import (
    DOMAIN_APPLICATION,
    DOMAIN_CONTAINER,
    DOMAIN_DATABASE,
    DOMAIN_NETWORK,
    DOMAIN_OS,
    DOMAIN_SECURITY,
    DOMAIN_SERVICES,
    DOMAIN_STORAGE,
)
from pkg.reasoning.evidence_cluster import LogCluster
from workers.advisory_analyst_handler import run_advisory_analyst
from workers.archivist import RecallResult

logger = logging.getLogger(__name__)

_DOMAIN_CONTEXT: dict[str, str] = {
    DOMAIN_OS: (
        "[DOMAIN: OS/SYSTEM]\n"
        "Focus: kernel OOM events, CPU/memory pressure, I/O wait, process failures.\n"
        "Layer 1 (OS) MUST be diagnosed before assuming a Kubernetes-level cause.\n"
    ),
    DOMAIN_NETWORK: (
        "[DOMAIN: NETWORK]\n"
        "Focus: DNS resolution, packet loss, TCP resets, port unreachability.\n"
        "Layer 2 (Network): verify routing, DNS, firewall before Layer 3 (K8s).\n"
    ),
    DOMAIN_STORAGE: (
        "[DOMAIN: STORAGE]\n"
        "Focus: disk capacity, I/O errors, filesystem corruption, RAID degradation.\n"
        "Critical when disk_pct > 95% or inode exhaustion — data loss risk.\n"
    ),
    DOMAIN_SERVICES: (
        "[DOMAIN: SYSTEMD SERVICES]\n"
        "Focus: service unit failures, crash loops, OOM kills, watchdog timeouts.\n"
        "Use journalctl and systemctl status to map restart patterns.\n"
    ),
    DOMAIN_CONTAINER: (
        "[DOMAIN: CONTAINER/POD LOGS]\n"
        "Focus: application panics, unhandled exceptions, OOM, connection failures.\n"
        "Ensure Layer 1 (host OS) and Layer 2 (network) are healthy first.\n"
    ),
    DOMAIN_DATABASE: (
        "[DOMAIN: DATABASE]\n"
        "Focus: replication lag, connection exhaustion, deadlocks, table corruption.\n"
        "MySQL/PG/Redis each have distinct failure signatures — match engine.\n"
    ),
    DOMAIN_APPLICATION: (
        "[DOMAIN: APPLICATION/HTTP]\n"
        "Focus: 5xx error rates, latency spikes, circuit breaker state, memory leaks.\n"
        "Correlate with deployment events and resource metrics.\n"
    ),
    DOMAIN_SECURITY: (
        "[DOMAIN: SECURITY]\n"
        "Focus: brute force, privilege escalation, unusual process activity, exfiltration.\n"
        "High confidence threshold required — correlate with auth logs before escalating.\n"
    ),
}


def _build_evidence_text(cluster: LogCluster, recall: RecallResult | None = None) -> str:
    """Construct evidence narrative for the LLM advisory call."""
    rep = cluster.representative
    alert_hint = (rep.get("alert_hint") or "").strip()
    raw = (rep.get("raw") or "").strip()
    extracted = rep.get("extracted_fact") or {}

    lines: list[str] = []

    domain_ctx = _DOMAIN_CONTEXT.get(cluster.domain, "")
    if domain_ctx:
        lines.append(domain_ctx)

    lines.append(f"[REMOTE AGENT EVIDENCE — domain={cluster.domain} lane={cluster.lane}]")
    lines.append(f"probe={cluster.probe}")
    lines.append(f"occurrence_count={cluster.count} (5-min window)")

    dist = ", ".join(f"{r}={n}" for r, n in sorted(cluster.results.items()))
    lines.append(f"result_distribution: {dist}")

    if alert_hint:
        lines.append(f"\nalert_hint: {alert_hint}")
    if raw:
        lines.append(f"\nraw_log: {raw[:500]}")
    if extracted:
        lines.append(f"\nextracted_metrics: {extracted}")

    if cluster.is_new:
        lines.append("\n[NOTE] First observation of this pattern — no prior baseline available.")
    if cluster.is_storm:
        lines.append(f"\n[WARNING] Log storm: {cluster.count} occurrences exceed threshold.")

    if recall is not None:
        lines.append(
            f"\n[PARTIAL RAG HINT — score={recall.top_score:.3f} (below routing threshold)]\n"
            f"{recall.advisory}"
        )

    return "\n".join(lines)


async def analyze_cluster(
    ctx: Any,
    cluster: LogCluster,
    recall: RecallResult | None = None,
) -> AnalystAdvisory | None:
    """Run LLM advisory analysis on a single LogCluster.

    Only called for UNKNOWN_RESEARCH route — RAG miss with urgent signals.
    Returns AnalystAdvisory or None on LLM failure.
    """
    evidence_text = _build_evidence_text(cluster, recall)
    trace = cluster.fingerprint

    payload: dict[str, Any] = {
        "evidence_source": "RemoteAgent",
        "domain": cluster.domain,
        "lane": cluster.lane,
        "probe": cluster.probe,
        "agent_ids": list(cluster.agent_ids),
    }

    logger.info(
        "[REMOTE-ADVISOR] fp=%s domain=%s evidence_len=%d",
        trace, cluster.domain, len(evidence_text),
    )

    try:
        advisory = await run_advisory_analyst(
            ctx,
            payload=payload,
            trace=trace,
            evidence_text=evidence_text,
        )
        if advisory:
            logger.info(
                "[REMOTE-ADVISOR] ok fp=%s verdict=%s confidence=%s",
                trace, advisory.verdict, advisory.confidence,
            )
        return advisory
    except Exception as exc:
        logger.warning("[REMOTE-ADVISOR] failed fp=%s err=%s", trace, exc)
        return None
