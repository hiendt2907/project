"""Remote agent processing pipeline — orchestrates Stages 2-6 for RemoteAgent evidence.

Called from evidence_consumer.reason_from_diagnostic_evidence when
ev_doc["evidence_source"] == "RemoteAgent".

Pipeline:
  Stage 2 — Cluster (upsert_cluster)
  Stage 3 — Triage (triage_cluster: RAG lookup + urgency)
  Stage 4 — Research (analyze_cluster: LLM, only for UNKNOWN_RESEARCH)
  Stage 5 — Learn (write_lessons: write to RAG)
  Stage 6 — Notify (Telegram for critical/high)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pkg.reasoning.domain_signals import detect_domain
from pkg.reasoning.evidence_cluster import upsert_cluster
from pkg.reasoning.evidence_fingerprint import fingerprint_evidence
from workers.handler_context import WorkerHandlerContext
from workers.remote_advisor import analyze_cluster
from workers.remote_diagnostic_archiver import write_lessons
from workers.remote_triage import triage_cluster
from workers.telegram_advisory_emitter import render_advisory_to_telegram
from workers.remote_diagnosis_emitter import emit_diagnosis_to_telegram

logger = logging.getLogger(__name__)

# Giữ strong reference tới background task để GC không thu hồi giữa chừng và
# exception không bị nuốt silently (CRAT write trong _run_diagnosis_and_notify).
_BG_DIAG_TASKS: set[asyncio.Task] = set()


def _track_bg_task(task: asyncio.Task) -> None:
    _BG_DIAG_TASKS.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _BG_DIAG_TASKS.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error(
                "[RAP] background diagnosis task failed: %s", t.get_name(),
                exc_info=t.exception(),
            )

    task.add_done_callback(_on_done)

_NOTIFY_TIERS = frozenset({"critical", "high"})
_RESEARCH_ROUTES = frozenset({"UNKNOWN_RESEARCH"})


async def handle_remote_agent_evidence(
    ctx: WorkerHandlerContext,
    ev_doc: dict[str, Any],
    trace: str,
) -> str:
    """End-to-end pipeline for a single remote-agent evidence item."""
    extracted = ev_doc.get("extracted_fact") or {}
    if isinstance(extracted, str):
        try:
            extracted = json.loads(extracted)
        except Exception:
            extracted = {}
    if not isinstance(extracted, dict):
        extracted = {}
    agent_id = str(extracted.get("agent_id") or "unknown-agent")
    probe = str(ev_doc.get("probe") or "unknown")
    alert_hint = str(ev_doc.get("alert_hint") or "")
    raw = str(ev_doc.get("raw") or "")
    lane = str(ev_doc.get("lane") or "")
    labels = {
        "alertname": str(ev_doc.get("alert_rule") or ""),
        "namespace": str(ev_doc.get("namespace") or ""),
        "evidence_source": str(ev_doc.get("evidence_source") or ""),
    }

    # ── Stage 2: Cluster ──────────────────────────────────────────────────
    result = str(ev_doc.get("result") or "PASSED")
    fp = fingerprint_evidence({"probe": probe, "result": result, "alert_hint": alert_hint, "raw": raw})
    domain = detect_domain(probe, alert_hint, raw, lane, labels=labels)

    try:
        cluster = await upsert_cluster(ctx.redis, agent_id, fp, ev_doc, domain)
    except Exception as exc:
        logger.warning("[RAP] cluster_upsert_failed trace=%s err=%s", trace, exc)
        return ""

    logger.info(
        "[RAP] cluster fp=%s domain=%s count=%d is_new=%s is_storm=%s",
        fp, domain, cluster.count, cluster.is_new, cluster.is_storm,
    )

    # ── Stage 3: Triage ───────────────────────────────────────────────────
    triage = await triage_cluster(ctx, cluster)

    logger.info(
        "[RAP] triage fp=%s route=%s urgency=%s",
        fp, triage.route, triage.urgency,
    )

    # ── Stage 4: Research — multi-turn diagnosis loop for urgent clusters ──
    # INVARIANT INV_NO_SINGLE_TURN: diagnosis loop runs minimum 2 turns.
    # Runs as background asyncio.Task so Kafka consumer is not blocked.
    #
    # Routing decision:
    #   UNKNOWN_RESEARCH + critical/high  → background multi-turn diagnosis loop
    #   KNOWN_BASELINE   + critical/high  → still diagnose (known pattern but urgent)
    #   any route        + medium/below   → fallback single-turn advisory
    advisory = None
    diag_task_launched = False
    # KNOWN_WITH_FIX may match K8s playbooks that are semantically wrong for
    # remote-agent evidence (disk/storage domain vs k8s executor tools).
    # Treat it identically to KNOWN_BASELINE for the research decision.
    needs_research = triage.route in _RESEARCH_ROUTES or (
        triage.route in ("KNOWN_BASELINE", "KNOWN_WITH_FIX") and triage.urgency in _NOTIFY_TIERS
    )
    if needs_research and triage.urgency in _NOTIFY_TIERS:
        chat_id = getattr(ctx, "telegram_chat_id", None) or getattr(
            ctx.settings, "telegram_admin_chat_id", None
        )
        llm = getattr(ctx, "llm", None)
        model = getattr(getattr(ctx, "settings", None), "diag_evidence_llm_model", None) or "qwen2.5-coder:7b"
        num_ctx = int(getattr(getattr(ctx, "settings", None), "llm_num_ctx", 8192) or 8192)

        if llm is not None and chat_id is not None:
            _track_bg_task(asyncio.create_task(
                _run_diagnosis_and_notify(
                    ctx=ctx,
                    ev_doc=ev_doc,
                    agent_id=agent_id,
                    trace=trace,
                    llm=llm,
                    model=model,
                    num_ctx=num_ctx,
                    chat_id=int(chat_id),
                ),
                name=f"diag-{trace[:12]}",
            ))
            diag_task_launched = True
            logger.info("[RAP] diagnosis_loop launched as background task trace=%s", trace)
        else:
            advisory = await analyze_cluster(ctx, cluster, recall=triage.recall)
    elif triage.route in _RESEARCH_ROUTES:
        advisory = await analyze_cluster(ctx, cluster, recall=triage.recall)

    # ── Stage 5: Learn — write to RAG ────────────────────────────────────
    await write_lessons(ctx, cluster, triage, advisory)

    # ── Stage 6: Notify — Telegram for fallback advisory (non-loop path) ──
    if advisory is not None and triage.urgency in _NOTIFY_TIERS:
        try:
            chat_id = getattr(ctx, "telegram_chat_id", None) or getattr(
                ctx.settings, "telegram_admin_chat_id", None
            )
            if chat_id is not None:
                import dataclasses
                if not hasattr(advisory, "trace_id") or not advisory.trace_id:
                    advisory = dataclasses.replace(advisory, trace_id=trace)
                await render_advisory_to_telegram(ctx, advisory, int(chat_id))
        except Exception as exc:
            logger.warning("[RAP] telegram_notify_failed trace=%s err=%s", trace, exc)

    verdict = advisory.verdict if advisory else ("diagnosis_loop_launched" if diag_task_launched else "no_advisory")
    logger.info(
        "[RAP] done trace=%s fp=%s route=%s urgency=%s verdict=%s",
        trace, fp, triage.route, triage.urgency, verdict,
    )
    return f"remote_agent:{triage.route}:{verdict}"


async def _run_diagnosis_and_notify(
    ctx: WorkerHandlerContext,
    ev_doc: dict,
    agent_id: str,
    trace: str,
    llm: Any,
    model: str,
    num_ctx: int,
    chat_id: int,
) -> None:
    """Background task: run multi-turn diagnosis loop then emit Telegram.

    INVARIANT INV_DIAG_STORED: session must be stored in Redis before emit.
    """
    from services.analyst.diagnosis_loop import run_diagnosis_loop
    try:
        session = await run_diagnosis_loop(
            redis=ctx.redis,
            llm_client=llm,
            agent_id=agent_id,
            ev_doc=ev_doc,
            trace_id=trace,
            model=model,
            num_ctx=num_ctx,
        )
        await emit_diagnosis_to_telegram(ctx, session, chat_id)
    except RuntimeError as exc:
        # INV_DIAG_STORED violated — do NOT emit Telegram
        logger.error("[RAP] diagnosis_aborted INV_DIAG_STORED trace=%s err=%s", trace, exc)
    except Exception as exc:
        logger.error("[RAP] diagnosis_loop_error trace=%s err=%s", trace, exc)
