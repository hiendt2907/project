"""Advisory Mode Analyst Handler — structured incident reports with forecasts (no mutations)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from pkg.reasoning.analyst_advisory_schema import AnalystAdvisory
from services.audit_ledger.chain_writer import write_audit_block
from services.audit_ledger.signer import AuditLedgerError
from workers.advisory_mode_system_prompt import build_advisory_system_prompt
from workers.handler_context import WorkerHandlerContext
from workers.llm_context_budget import effective_reply_max_words
from workers.llm_trace import log_llm_trace
from workers.metrics_exporter import inc_llm_requests
from workers.request_trace import log_end_request_ctx, log_start_request_ctx

logger = logging.getLogger(__name__)


async def run_advisory_analyst(
    ctx: WorkerHandlerContext,
    payload: dict[str, Any],
    trace: str,
    evidence_text: str,
) -> AnalystAdvisory | None:
    """
    Advisory Mode Analyst: read-only diagnosis + structured forecast.

    Args:
        ctx: Handler context with LLM + settings
        payload: Inbound payload (chat_id, namespace, etc.)
        trace: Trace ID
        evidence_text: Sanitized evidence narrative (includes [TEMPORAL_EVIDENCE] blocks if available)

    Returns:
        AnalystAdvisory object or None on failure
    """
    if not evidence_text.strip():
        logger.warning("event=advisory_analyst_empty_evidence trace=%s", trace)
        return None

    t0 = time.perf_counter()
    chat_id = payload.get("chat_id")
    log_start_request_ctx(
        phase="advisory_analyst",
        source="evidence_inbound",
        chat_id=chat_id,
        text_len=len(evidence_text),
        in_preview=evidence_text[:800] if evidence_text else "",
    )

    err: BaseException | None = None
    advisory: AnalystAdvisory | None = None

    try:
        ws = ctx.settings
        model = getattr(ws, "model_reasoning_engine", "ollama") or "ollama"
        dm = getattr(ws, "diag_evidence_llm_model", None) or ""
        if isinstance(dm, str) and dm.strip():
            model = dm.strip()

        system_prompt = build_advisory_system_prompt(ws)
        max_words = effective_reply_max_words(ws)

        inc_llm_requests()
        log_llm_trace(
            ws,
            trace=trace,
            phase="advisory_analyst_prompt_contract",
            model=model,
            parse_ok=True,
            detail=(
                f"system_len={len(system_prompt)} user_len={len(evidence_text[:24000])} "
                f"max_words={max_words} temperature=0.2 num_predict=2048"
            ),
            raw_response=(
                "[SYSTEM_EXCERPT]\n"
                f"{system_prompt[:1600]}\n\n"
                "[USER_EXCERPT]\n"
                f"{evidence_text[:1600]}"
            ),
        )

        resp = await ctx.llm.chat(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt[:16000]},
                {"role": "user", "content": evidence_text[:24000]},
            ],
            options={"num_predict": 2048, "temperature": 0.2},
        )

        raw_llm = str(((resp or {}).get("message") or {}).get("content") or "").strip()
        if not raw_llm:
            logger.warning("event=advisory_analyst_empty_response trace=%s", trace)
            log_llm_trace(
                ws,
                trace=trace,
                phase="advisory_analyst_empty",
                model=model,
                raw_response="",
                parse_ok=False,
                detail="LLM returned empty content",
            )
            return None

        # Parse JSON response
        parsed = _parse_advisory_json(raw_llm)
        if not parsed:
            logger.warning("event=advisory_analyst_parse_failed trace=%s", trace)
            log_llm_trace(
                ws,
                trace=trace,
                phase="advisory_analyst_parse_failed",
                model=model,
                raw_response=raw_llm[:1600],
                parse_ok=False,
                detail="Could not parse response as JSON",
            )
            return None

        # Inject trace_id if missing
        if not parsed.get("trace_id"):
            parsed["trace_id"] = trace

        # Validate schema
        try:
            advisory = AnalystAdvisory(**parsed)
        except Exception as e:
            logger.warning("event=advisory_analyst_schema_error trace=%s err=%s", trace, e)
            log_llm_trace(
                ws,
                trace=trace,
                phase="advisory_analyst_schema_error",
                model=model,
                raw_response=raw_llm[:1600],
                parse_ok=False,
                detail=f"Schema validation failed: {e!s}"[:500],
            )
            return None

        log_llm_trace(
            ws,
            trace=trace,
            phase="advisory_analyst_ok",
            model=model,
            raw_response=raw_llm[:1600],
            parse_ok=True,
            detail=(
                f"verdict={advisory.verdict} confidence={advisory.confidence} "
                f"verification_steps={len(advisory.verification_steps)} "
                f"remediation_steps={len(advisory.proposed_remediation)}"
            ),
        )

        # CRAT: fail-closed — audit write MUST succeed before advisory is returned.
        try:
            await write_audit_block(
                event_type="ADVISORY_DECISION",
                trace_id=trace,
                payload=advisory.model_dump(),
                redis=ctx.redis,
                kafka=ctx.kafka,
                kafka_topic=getattr(ws, "kafka_topic_audit_chain", "omni-audit-chain"),
            )
        except AuditLedgerError as _audit_err:
            logger.critical(
                "event=audit_chain_write_failed phase=advisory_analyst trace=%s err=%s FAIL_CLOSED",
                trace,
                _audit_err,
            )
            advisory = None
            return None

        return advisory

    except BaseException as e:
        err = e
        logger.exception("event=advisory_analyst_error trace=%s", trace)
        raise

    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        status = "error" if err else ("ok" if advisory else "parse_failed")
        out_preview = (
            f"verdict={advisory.verdict}"
            if advisory
            else (f"{type(err).__name__}" if err else "no_advisory")
        )
        log_end_request_ctx(
            phase="advisory_analyst",
            status=status,
            duration_ms=ms,
            out_len=len(advisory.model_dump_json()) if advisory else 0,
            out_preview=out_preview,
            error=f"{type(err).__name__}: {err}" if err else None,
        )


def _parse_advisory_json(raw: str) -> dict[str, Any] | None:
    """Extract JSON object from LLM response."""
    s = (raw or "").strip()
    if not s:
        return None
    # Try to find JSON object boundaries
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        parsed = json.loads(s[i : j + 1])
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None
