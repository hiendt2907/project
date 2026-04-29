"""Advisory Mode Analyst Handler — structured incident reports with forecasts (no mutations)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

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

_LLM_TRANSIENT_ERRORS = (ConnectionError, TimeoutError, OSError)


@retry(
    retry=retry_if_exception_type(_LLM_TRANSIENT_ERRORS),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
async def _llm_chat_with_retry(llm: Any, model: str, messages: list, options: dict) -> dict:
    """Wrap ctx.llm.chat with exponential-backoff retry for transient network errors.

    Scope: only the HTTP call. CRAT writes are outside this scope.
    Deterministic failures (bad JSON, schema errors) bubble immediately without retry.
    """
    return await llm.chat(model=model, messages=messages, options=options)


# SIEM categories that are infrastructure incidents — NOT security threats.
# If the LLM incorrectly applies a "Security incident" escalation to these,
# we correct it post-hoc because small LLMs over-escalate on SIEM source.
_INFRA_SIEM_CATEGORIES = frozenset({
    "high_cpu", "high_mem", "disk_pressure", "db_crash",
    "network_timeout", "service_unavailable", "oom", "latency_spike",
})


def _correct_escalation_reason(advisory: AnalystAdvisory, evidence_text: str) -> AnalystAdvisory:
    """
    Guardrail: if the evidence is an infra SIEM category but the LLM incorrectly
    applied a security-incident escalation template, clear the escalation_reason.
    """
    if not advisory.escalation_reason:
        return advisory
    if "security incident" not in advisory.escalation_reason.lower():
        return advisory
    # Check if any infra category appears in the evidence
    ev_lower = evidence_text.lower()
    if any(cat in ev_lower for cat in _INFRA_SIEM_CATEGORIES):
        logger.warning(
            "event=escalation_correction_applied trace=advisory "
            "reason='infra category misclassified as security incident' "
            "original_reason=%r",
            advisory.escalation_reason[:100],
        )
        return advisory.model_copy(update={"escalation_reason": ""})
    return advisory


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

        resp = await _llm_chat_with_retry(
            ctx.llm,
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

        # Guardrail: correct LLM escalation misclassification for infra SIEM categories.
        advisory = _correct_escalation_reason(advisory, evidence_text)

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
