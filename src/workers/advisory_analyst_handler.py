"""Advisory Mode Analyst Handler — structured incident reports with forecasts (no mutations)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from pkg.reasoning.analyst_advisory_schema import (
    AnalystAdvisory,
    DEFAULT_ACTION_FALLBACK,
    DEFAULT_COMMAND_FALLBACK,
    DEFAULT_CONFIDENCE,
    DEFAULT_FORECAST_METHOD,
    DEFAULT_LAYER,
    DEFAULT_VERDICT,
    VALID_CONFIDENCE_LEVELS,
    VALID_VERDICTS,
    normalize_evidence_lane,
    normalize_layer,
)
from services.audit_ledger.chain_writer import write_audit_block
from services.audit_ledger.signer import AuditLedgerError
from workers.advisory_mode_system_prompt import build_advisory_system_prompt
from workers.handler_context import WorkerHandlerContext
from workers.llm_context_budget import effective_reply_max_words, truncate_for_llm
from workers.llm_trace import log_llm_trace
from workers.metrics_exporter import inc_llm_requests
from workers.request_trace import log_end_request_ctx, log_start_request_ctx

logger = logging.getLogger(__name__)

_LLM_TRANSIENT_ERRORS = (ConnectionError, TimeoutError, OSError)


_LLM_CHAT_TIMEOUT_SEC = 120.0  # fallback only; align with WorkerSettings.llm_chat_timeout_sec default

_DEFAULT_FORECAST: dict[str, Any] = {
    "method": DEFAULT_FORECAST_METHOD,
    "basis": "insufficient evidence for quantitative extrapolation",
    "forecasts": [
        {"timeframe": "1h", "severity": "degraded", "prediction": "issue persists without intervention", "confidence": "low"},
        {"timeframe": "3h", "severity": "degraded", "prediction": "ongoing degradation likely", "confidence": "low"},
        {"timeframe": "6h", "severity": "critical", "prediction": "service impact escalates without fix", "confidence": "low"},
        {"timeframe": "12h", "severity": "critical", "prediction": "cascading failures possible", "confidence": "low"},
        {"timeframe": "24h", "severity": "catastrophic", "prediction": "full outage risk without intervention", "confidence": "low"},
    ],
    "note": "heuristic fallback — model did not emit forecast",
}


def _repair_advisory_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Normalize qwen2.5-coder:7b output to AnalystAdvisory schema before Pydantic validation.

    Repairs: LAYER_N aliases, commands[] → command str, description → rationale,
    missing order/verdict/confidence/proposed_remediation/forecast.
    """
    d = dict(d)  # never mutate caller's dict
    # Top-level required literals
    if d.get("verdict") not in VALID_VERDICTS:
        d["verdict"] = DEFAULT_VERDICT
    if d.get("confidence") not in VALID_CONFIDENCE_LEVELS:
        d["confidence"] = DEFAULT_CONFIDENCE

    # verification_steps
    steps: list[Any] = d.get("verification_steps") or []
    fixed: list[dict[str, Any]] = []
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            continue
        # order
        if not isinstance(step.get("order"), int) or step.get("order", 0) < 1:
            step["order"] = i
        # layer alias normalisation (substring-based for verbose model output)
        step["layer"] = normalize_layer(str(step.get("layer") or DEFAULT_LAYER))
        # command: prefer single string; fall back to joining commands[]
        if not step.get("command"):
            cmds: Any = step.get("commands") or []
            if isinstance(cmds, list) and cmds:
                step["command"] = "; ".join(str(c) for c in cmds[:3])
            elif cmds:
                step["command"] = str(cmds)
            else:
                step["command"] = DEFAULT_COMMAND_FALLBACK
        # rationale: fall back to description / reason
        if not step.get("rationale"):
            step["rationale"] = (
                step.get("description") or step.get("reason") or "no rationale provided"
            )
        fixed.append(step)
    d["verification_steps"] = fixed

    # proposed_remediation
    rems: list[Any] = d.get("proposed_remediation") or []
    fixed_rems: list[dict[str, Any]] = []
    for i, rem in enumerate(rems, 1):
        if not isinstance(rem, dict):
            continue
        if not isinstance(rem.get("order"), int) or rem.get("order", 0) < 1:
            rem["order"] = i
        if not rem.get("action"):
            rem["action"] = rem.get("description") or rem.get("command") or DEFAULT_ACTION_FALLBACK
        fixed_rems.append(rem)
    d["proposed_remediation"] = fixed_rems

    # impact_chain — optional cross-tier causal chain. Drop malformed links,
    # normalize evidence_lane to a canonical value, supply safe defaults.
    chain: list[Any] = d.get("impact_chain") or []
    fixed_chain: list[dict[str, Any]] = []
    if isinstance(chain, list):
        for link in chain:
            if not isinstance(link, dict):
                continue
            cause = str(link.get("cause") or "").strip()
            effect = str(link.get("effect") or "").strip()
            # A link without both a cause and an effect carries no causal signal.
            if not cause or not effect:
                continue
            link["cause"] = cause
            link["effect"] = effect
            link["mechanism"] = str(link.get("mechanism") or "propagation mechanism unspecified").strip()
            link["evidence_lane"] = normalize_evidence_lane(str(link.get("evidence_lane") or "state"))
            if link.get("confidence") not in VALID_CONFIDENCE_LEVELS:
                link["confidence"] = "medium"
            fixed_chain.append(link)
    d["impact_chain"] = fixed_chain

    # forecast — repair if missing or lacks required method/forecasts fields
    fc = d.get("forecast")
    if (
        not fc
        or not isinstance(fc, dict)
        or not fc.get("method")
        or not isinstance(fc.get("forecasts"), list)
        or len(fc.get("forecasts") or []) == 0
    ):
        d["forecast"] = _DEFAULT_FORECAST.copy()
    else:
        _SEVERITY_ALIASES = {
            "severe": "critical",
            "warning": "degraded",
            "info": "healthy",
            "ok": "healthy",
            "none": "healthy",
        }
        _VALID_SEVERITY = {"healthy", "degraded", "critical", "catastrophic"}
        _TF_ALIASES = {"1d": "24h", "24hours": "24h", "half_day": "12h", "6hours": "6h", "3hours": "3h", "1hour": "1h"}
        _VALID_TF = {"1h", "3h", "6h", "12h", "24h"}
        fc_list: list[Any] = fc.get("forecasts") or []
        for fitem in fc_list:
            if not isinstance(fitem, dict):
                continue
            sv = str(fitem.get("severity") or "").strip().lower()
            if sv not in _VALID_SEVERITY:
                fitem["severity"] = _SEVERITY_ALIASES.get(sv, "degraded")
            tf = str(fitem.get("timeframe") or "").strip().lower()
            if tf not in _VALID_TF:
                fitem["timeframe"] = _TF_ALIASES.get(tf, "24h")

    return d


@retry(
    retry=retry_if_exception_type(_LLM_TRANSIENT_ERRORS),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
async def _llm_chat_with_retry(
    llm: Any, model: str, messages: list, options: dict, *, timeout: float = _LLM_CHAT_TIMEOUT_SEC
) -> dict:
    """Wrap ctx.llm.chat with exponential-backoff retry and per-call timeout.

    Scope: only the HTTP call. CRAT writes are outside this scope.
    Deterministic failures (bad JSON, schema errors) bubble immediately without retry.
    asyncio.TimeoutError is not retried (it's not in _LLM_TRANSIENT_ERRORS).
    """
    return await asyncio.wait_for(
        llm.chat(model=model, messages=messages, options=options, format="json"),
        timeout=timeout,
    )


# SIEM categories that are infrastructure incidents — NOT security threats.
# If the LLM incorrectly applies a "Security incident" escalation to these,
# we correct it post-hoc because small LLMs over-escalate on SIEM source.
_INFRA_SIEM_CATEGORIES = frozenset({
    "high_cpu", "high_mem", "disk_pressure", "db_crash",
    "network_timeout", "service_unavailable", "oom", "latency_spike",
})


def _compute_escalation_tier(adv: AnalystAdvisory) -> str:
    """Determine L1/L2/L3 escalation tier from advisory fields.

    L1_AUTO: high confidence + URGENT/CRITICAL verdict + no step requires approval + no escalation_reason
    L3_HITL: low confidence OR NORMAL verdict OR escalation_reason set (novel/security/out-of-scope)
    L2_SUGGEST: everything else (the safe default)
    """
    needs_approval = any(s.approval_required for s in adv.proposed_remediation)
    if (
        adv.confidence == "high"
        and adv.verdict in {"URGENT", "CRITICAL"}
        and not needs_approval
        and not adv.escalation_reason
    ):
        return "L1_AUTO"
    if adv.confidence == "low" or adv.verdict == "NORMAL" or bool(adv.escalation_reason):
        return "L3_HITL"
    return "L2_SUGGEST"


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
        num_predict = int(getattr(ws, "omni_advisory_num_predict", 1024))
        _raw_ctx = getattr(ws, "llm_num_ctx", None)
        num_ctx = int(_raw_ctx) if isinstance(_raw_ctx, (int, float)) else 4096
        # Budget: (num_ctx - num_predict) tokens for input. ~4 chars/token conservative estimate.
        input_token_budget = max(512, num_ctx - num_predict)
        input_char_budget = input_token_budget * 4
        user_evidence_budget = min(10_000, int(input_char_budget * 0.65))
        system_prompt_max_chars = int(input_char_budget * 0.35)
        full_evidence_len = len(evidence_text)
        _reminder = (
            '\n\nREMINDER: Output ONLY a JSON object. '
            'Required fields: trace_id, verdict, root_cause (1-sentence concrete fact), '
            'confidence, affected_workload, verification_steps, proposed_remediation, forecast. '
            'For multi-tier faults ALSO include impact_chain (cause→mechanism→effect→evidence_lane). '
            'Do NOT echo the evidence structure. Do NOT output layer names as root_cause.'
        )
        user_blob = truncate_for_llm(evidence_text, user_evidence_budget - len(_reminder), tail=True) + _reminder
        if full_evidence_len > user_evidence_budget:
            logger.info(
                "event=advisory_ctx_budget trace=%s clipped=user_evidence tail=true "
                "original_len=%s budget_chars=%s",
                trace,
                full_evidence_len,
                user_evidence_budget,
            )
        system_excerpt = system_prompt[:system_prompt_max_chars]
        if len(system_prompt) > system_prompt_max_chars:
            logger.info(
                "event=advisory_ctx_budget trace=%s clipped=system_prompt_head "
                "original_len=%s budget_chars=%s",
                trace,
                len(system_prompt),
                system_prompt_max_chars,
            )

        logger.info(
            "event=advisory_llm_budget trace=%s model=%s num_predict=%s "
            "system_len=%s user_len=%s llm_timeout_sec=%s",
            trace,
            model,
            num_predict,
            len(system_excerpt),
            len(user_blob),
            float(getattr(ctx.settings, "llm_chat_timeout_sec", _LLM_CHAT_TIMEOUT_SEC)),
        )

        inc_llm_requests()
        log_llm_trace(
            ws,
            trace=trace,
            phase="advisory_analyst_prompt_contract",
            model=model,
            parse_ok=True,
            detail=(
                f"system_len={len(system_excerpt)} user_len={len(user_blob)} "
                f"max_words={max_words} temperature=0.2 num_predict={num_predict}"
            ),
            raw_response=(
                "[SYSTEM_EXCERPT]\n"
                f"{system_excerpt[:1600]}\n\n"
                "[USER_EXCERPT]\n"
                f"{user_blob[:1600]}"
            ),
        )

        llm_timeout = float(getattr(ctx.settings, "llm_chat_timeout_sec", _LLM_CHAT_TIMEOUT_SEC))
        resp = await _llm_chat_with_retry(
            ctx.llm,
            model=model,
            messages=[
                {"role": "system", "content": system_excerpt},
                {"role": "user", "content": user_blob},
            ],
            options={"num_predict": num_predict, "temperature": 0.0, "num_ctx": num_ctx, "think": False},
            timeout=llm_timeout,
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

        # Bind real correlation id (LLM often emits a placeholder trace_id).
        parsed["trace_id"] = trace

        # Normalize qwen2.5-coder output before Pydantic validation.
        parsed = _repair_advisory_dict(parsed)

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

        # Compute escalation tier from corrected advisory fields (immutable update).
        tier = _compute_escalation_tier(advisory)
        advisory = advisory.model_copy(update={"escalation_tier": tier})

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

        # S1.4: hash raw LLM output for compliance audit, store short-TTL in Redis.
        llm_reasoning_hash = hashlib.sha256(raw_llm.encode()).hexdigest()
        llm_reasoning_ref = f"omni:crat:llm_reason:{trace}:advisory"
        try:
            await ctx.redis.setex(llm_reasoning_ref, 86400, raw_llm)
        except Exception as _hash_err:
            logger.warning("event=llm_reason_store_fail trace=%s err=%s", trace, _hash_err)

        # CRAT: fail-closed — audit write MUST succeed before advisory is returned.
        audit_payload = advisory.model_dump()
        audit_payload["llm_reasoning_hash"] = llm_reasoning_hash
        audit_payload["llm_reasoning_ref"] = llm_reasoning_ref
        try:
            await write_audit_block(
                event_type="ADVISORY_DECISION",
                trace_id=trace,
                payload=audit_payload,
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

        # CRAT boundary: only return after ADVISORY_DECISION write — downstream Telegram assumes this ordering.
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
