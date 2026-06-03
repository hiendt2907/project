"""Remote diagnostic lesson writer — Stage 5 of the SRE cognitive pipeline.

Writes to two RAG collections after every cluster is processed:

  COLLECTION_DIAGNOSTIC_HISTORY  — ALL clusters including baseline
    Purpose: trend analysis, anomaly detection baseline, frequency tracking

  COLLECTION_ACTION_EXPERIENCE   — Only clusters with LLM advisory
    Purpose: RAG recall on next sighting (skips LLM on cache hit)
    memory_kind = "remote_diagnostic"
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from pkg.reasoning.analyst_advisory_schema import AnalystAdvisory
from pkg.reasoning.evidence_cluster import LogCluster
from rag.redis_vector_store import (
    COLLECTION_ACTION_EXPERIENCE,
    COLLECTION_DIAGNOSTIC_HISTORY,
    PointStruct,
)
from workers.remote_triage import TriageResult, _build_symptom_text

logger = logging.getLogger(__name__)


def _embedding_from_response(resp: dict[str, Any]) -> list[float]:
    if "embedding" in resp:
        emb = resp["embedding"]
        return list(emb) if not isinstance(emb, list) else emb
    embs = resp.get("embeddings")
    if isinstance(embs, list) and embs:
        return list(embs[0])
    raise ValueError("embed response missing embedding(s)")


async def _embed(ctx: Any, text: str) -> list[float]:
    """Embed text using ctx.llm; return float vector."""
    llm = getattr(ctx, "llm", None)
    ws = getattr(ctx, "settings", None)
    if llm is None or ws is None:
        raise RuntimeError("embed requires ctx.llm and ctx.settings")
    embed_model = str(getattr(ws, "embed_model", "nomic-embed-text") or "nomic-embed-text")
    resp = await llm.embed(model=embed_model, input=text[:6000])
    return _embedding_from_response(resp)


async def write_lessons(
    ctx: Any,
    cluster: LogCluster,
    triage: TriageResult,
    advisory: AnalystAdvisory | None,
) -> None:
    """Write diagnostic lessons to RAG. Called regardless of advisory outcome.

    Baseline clusters are still written to COLLECTION_DIAGNOSTIC_HISTORY
    so future trend analysis can detect deviation from normal.
    """
    vs = getattr(ctx, "vector_store", None)
    if vs is None:
        logger.debug("[ARCHIVER] skip — no vector_store in ctx")
        return

    symptom_text = _build_symptom_text(cluster)
    now_ts = str(int(time.time()))

    try:
        vector = await _embed(ctx, symptom_text)
    except Exception as exc:
        logger.warning("[ARCHIVER] embed failed fp=%s err=%s", cluster.fingerprint, exc)
        return

    # ── 1. COLLECTION_DIAGNOSTIC_HISTORY — always write ──────────────────
    history_payload: dict[str, Any] = {
        "fingerprint": cluster.fingerprint,
        "domain": cluster.domain,
        "lane": cluster.lane,
        "probe": cluster.probe,
        "count": cluster.count,
        "result_distribution": dict(cluster.results),
        "urgency": triage.urgency,
        "route": triage.route,
        "is_new_pattern": cluster.is_new,
        "is_storm": cluster.is_storm,
        "agent_ids": list(cluster.agent_ids),
        "advisory_verdict": advisory.verdict if advisory else None,
        "advisory_root_cause": advisory.root_cause[:200] if advisory else None,
        "symptom_summary": symptom_text[:500],
        "ts": now_ts,
        "memory_kind": "remote_diagnostic_history",
        # Required by vector store text_content field
        "text": symptom_text[:2000],
    }
    try:
        await vs.upsert(
            COLLECTION_DIAGNOSTIC_HISTORY,
            [PointStruct(id=str(uuid.uuid4()), vector=vector, payload=history_payload)],
        )
        logger.debug(
            "[ARCHIVER] history_written fp=%s domain=%s urgency=%s",
            cluster.fingerprint, cluster.domain, triage.urgency,
        )
    except Exception as exc:
        logger.warning(
            "[ARCHIVER] history_write_failed fp=%s err=%s", cluster.fingerprint, exc
        )

    # ── 2. COLLECTION_ACTION_EXPERIENCE — only with advisory ─────────────
    if advisory is None:
        return

    lesson_text = (
        f"[remote] domain={cluster.domain} lane={cluster.lane} "
        f"pattern_count={cluster.count} "
        f"root_cause={advisory.root_cause[:200]} "
        f"verdict={advisory.verdict}"
    )
    rep_trace = cluster.representative.get("trace_id") or ""
    experience_payload: dict[str, Any] = {
        "memory_kind": "remote_diagnostic",
        "symptom_text": symptom_text[:2000],
        "workload_fingerprint": f"{cluster.domain}:{next(iter(cluster.agent_ids), 'unknown')}",
        "lesson": lesson_text,
        "advisory_verdict": advisory.verdict,
        "advisory_root_cause": advisory.root_cause,
        "advisory_confidence": advisory.confidence,
        "domain": cluster.domain,
        "lane": cluster.lane,
        "probe": cluster.probe,
        "fingerprint": cluster.fingerprint,
        "occurrence_count": cluster.count,
        # "tool" explicitly set so archivist reads advisory_only → KNOWN_BASELINE route.
        # Without this, top_tool="" which has the same effect but less explicit.
        "tool": "advisory_only",
        "exec_outcome": "advisory_only",
        "biz_outcome": "pending_verification",
        "trace_id": rep_trace,
        "ts": now_ts,
        # Required by vector store text_content field
        "text": lesson_text,
        "summary": advisory.root_cause[:300],
    }
    try:
        await vs.upsert(
            COLLECTION_ACTION_EXPERIENCE,
            [PointStruct(id=str(uuid.uuid4()), vector=vector, payload=experience_payload)],
        )
        logger.info(
            "[ARCHIVER] experience_written fp=%s verdict=%s domain=%s",
            cluster.fingerprint, advisory.verdict, cluster.domain,
        )
    except Exception as exc:
        logger.warning(
            "[ARCHIVER] experience_write_failed fp=%s err=%s", cluster.fingerprint, exc
        )
