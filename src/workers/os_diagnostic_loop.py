"""Iterative OS diagnostic loop for SYS_HARD_FAIL alerts (Lane 2).

RAG-guided loop: each iteration queries os_hard_fail_diagnostic for the next
probe to run, executes it, appends to a sliding context window, and repeats
until a terminal pair is found or max_iterations is reached.

Returns a contrast string (same contract as compare_alert_claim_to_os_state)
or None (fall through to LLM).
"""

from __future__ import annotations

import logging
from typing import Any

from rag.redis_vector_store import COLLECTION_OS_HARD_FAIL_DIAGNOSTIC
from workers.os_state_validator import (
    _OS_PROBE_HANDLERS,
    _alert_ctx_summary,
    _parse_ef,
    _probe_result,
    _sanitize_probe_ev,
)

logger = logging.getLogger(__name__)

_SCORE_THRESHOLD = 0.55
_SLIDING_WINDOW = 2


def _build_sliding_window_q(
    alert_ctx: dict[str, Any],
    context_stack: list[tuple[str, str | None]],
) -> str:
    """Build a sliding-window query string (max 400 chars) for RAG lookup."""
    an = alert_ctx.get("alertname", "")
    sev = (alert_ctx.get("labels") or {}).get("severity", "")
    ns = alert_ctx.get("namespace", "")
    src = alert_ctx.get("source", "")
    header = f"alert={an} severity={sev} ns={ns} source={src}"
    step_lines: list[str] = []
    for i, (probe_name, finding) in enumerate(context_stack, start=1):
        # Normalize to match ingest Q format exactly — None means anomaly found
        normalized = "PASSED-no-anomaly" if finding is not None else "FAILED-or-anomaly"
        step_lines.append(f"step{i}: probe={probe_name} result={normalized}")
    q = "\n".join([header] + step_lines)
    return q[:400]


def _run_handler_or_generic(
    handler: Any,
    ev: dict[str, Any],
    alert_ctx: dict[str, Any],
) -> str | None:
    """Run registered handler or fall back to a generic probe description."""
    sanitized = _sanitize_probe_ev(ev)
    if handler is not None:
        try:
            return handler(sanitized, alert_ctx)
        except Exception as exc:
            logger.warning("os_diagnostic_loop: handler=%s raised err=%r",
                           getattr(handler, '__name__', '?'), exc)
            return None
    # generic fallback for unregistered probes
    result = _probe_result(ev)
    if result == "PASSED":
        ef = _parse_ef(ev.get("extracted_fact"), "")
        anomaly_words = ("error", "fail", "critical", "down", "missing", "issue")
        anomaly_values = [
            ef[k] for k in ef
            if any(w in k.lower() for w in anomaly_words) and ef[k]
        ]
        if not anomaly_values:
            probe_name = ev.get("probe", "unknown")
            return f"PASSED-no-anomaly (probe={probe_name})"
    return f"FAILED-or-anomaly (result={result})"


def _format_terminal_contrast(pair: dict[str, Any], alert_ctx: dict[str, Any]) -> str:
    """Format a terminal RAG pair into a contrast string for evidence_consumer."""
    ctx_summary = _alert_ctx_summary(alert_ctx)
    root_cause = pair.get("root_cause", "")
    fix = pair.get("fix", "")
    interpretation = pair.get("interpretation", "")
    domain = pair.get("domain", "")
    confidence = pair.get("confidence", 0.0)

    parts = [f"[OS_DIAGNOSTIC_LOOP] terminal pair matched (domain={domain}, confidence={confidence:.2f})."]
    if ctx_summary:
        parts.append(f"Alert: {ctx_summary}.")
    if root_cause:
        parts.append(f"Root cause: {root_cause}.")
    if fix:
        parts.append(f"Fix: {fix}.")
    if interpretation:
        parts.append(f"Interpretation: {interpretation}.")
    return " ".join(parts)


async def run_os_diagnostic_loop(
    ctx: Any,
    batch: list[dict[str, Any]],
    by_probe: dict[str, dict[str, Any]],
    alert_ctx: dict[str, Any],
    trace: Any,
    *,
    max_iterations: int = 8,
) -> str | None:
    """Run the iterative OS diagnostic loop.

    Queries os_hard_fail_diagnostic RAG collection each iteration to get the
    next probe suggestion. Runs the probe, appends finding to a sliding window,
    and continues. Stops when:
    - A terminal pair is returned (confident diagnosis found)
    - RAG score < threshold (miss → fall through to LLM)
    - max_iterations reached

    Returns contrast string on terminal match, None otherwise.
    """
    if not hasattr(ctx, "vector_store") or ctx.vector_store is None:
        logger.debug("os_diagnostic_loop: no vector_store, skipping")
        return None

    ws = getattr(ctx, "settings", None)
    llm = getattr(ctx, "llm", None)
    if llm is None or ws is None:
        logger.debug("os_diagnostic_loop: no llm/settings, skipping")
        return None

    embed_model = getattr(ws, "embed_model", "nomic-embed-text:latest")
    context_stack: list[tuple[str, str | None]] = []

    if not alert_ctx.get("alertname"):
        logger.warning("os_diagnostic_loop: empty alert_ctx, RAG recall degraded trace=%s", trace)

    for iteration in range(max_iterations):
        q = _build_sliding_window_q(alert_ctx, context_stack)

        try:
            resp = await ctx.vector_store.similarity_search(
                query=q,
                collection_id=COLLECTION_OS_HARD_FAIL_DIAGNOSTIC,
                llm=llm,
                embed_model=embed_model,
                limit=1,
                score_threshold=_SCORE_THRESHOLD,
            )
        except Exception as exc:
            logger.warning("os_diagnostic_loop: rag query failed iter=%d err=%r", iteration, exc)
            break

        if not resp.points:
            logger.debug("os_diagnostic_loop: rag miss iter=%d trace=%s", iteration, trace)
            break

        point = resp.points[0]
        pair = point.payload or {}
        score = point.score or 0.0

        logger.debug(
            "os_diagnostic_loop iter=%d pair_type=%s score=%.3f trace=%s",
            iteration, pair.get("pair_type"), score, trace,
        )

        if pair.get("pair_type") == "terminal":
            if not pair.get("root_cause"):
                logger.warning(
                    "os_diagnostic_loop: terminal pair missing root_cause, treating as miss iter=%d trace=%s",
                    iteration, trace,
                )
                break
            logger.info(
                "event=os_diagnostic_terminal_match iter=%d score=%.3f trace=%s",
                iteration, score, trace,
            )
            return _format_terminal_contrast(pair, alert_ctx)

        # entry / mid: run the suggested probe
        probe_name = pair.get("next_check")
        if not probe_name:
            logger.debug("os_diagnostic_loop: no next_check in pair, stopping iter=%d", iteration)
            break

        ev = by_probe.get(probe_name)
        if ev is None:
            logger.info("os_diagnostic_loop: probe=%s not in evidence, stopping iter=%d trace=%s",
                        probe_name, iteration, trace)
            break

        handler = _OS_PROBE_HANDLERS.get(probe_name)
        finding = _run_handler_or_generic(handler, ev, alert_ctx)

        context_stack.append((probe_name, finding))
        context_stack = context_stack[-_SLIDING_WINDOW:]

    return None
