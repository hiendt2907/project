"""Consume ``omni-diagnostic-evidence`` — read-only reasoning; emit SUGGEST_REMEDIATION to omni-actions."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pkg.rag.gate import evaluate_rag_gate
from pkg.reasoning import coerce_evidence_dict
from pkg.reasoning.sanitize import (
    evidence_relevance_warning,
    format_batch_sanitized_analyst_user_text,
    format_sanitized_analyst_user_text,
)
from workers.diagnostic_analyst_hard_logic import apply_sdk_truth_hard_logic
from workers.evidence_batch import append_evidence_and_take_flush_batch
from workers.handler_context import WorkerHandlerContext
from workers.omni_actions_remediation import build_suggest_remediation_body
from workers.reasoning_evidence_inbound import reason_diagnostic_evidence_only
from workers.request_trace import pop_trace_id, push_trace_id
from workers.telegram_outbound import send_telegram_out_for_inbound
from workers import ollama_prompts_en as ope

logger = logging.getLogger(__name__)

_NS_POD = re.compile(
    r"\bnamespace[=:]\s*([\w.-]+)|\bns[=:]\s*([\w.-]+)|\bpod[=:]\s*([\w.-]+)",
    re.I,
)


def _hints_from_evidence_text(text: str) -> dict[str, str] | None:
    """Best-effort namespace/pod from sanitized text for RagGate GIGO."""
    t = (text or "")[:12000]
    h: dict[str, str] = {}
    for m in _NS_POD.finditer(t):
        g = [x for x in m.groups() if x]
        if not g:
            continue
        val = g[0].strip()
        if not val:
            continue
        frag = m.group(0).lower()
        if "pod" in frag and "namespace" not in frag and "ns" not in frag:
            h.setdefault("pod_name", val)
        else:
            h.setdefault("namespace", val)
    return h if h else None


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


async def _emit_suggest_remediation(
    ctx: WorkerHandlerContext,
    *,
    trace: str,
    diagnosis: str,
    confidence: float,
    source: str,
    suggested_tool: str,
) -> None:
    if not ctx.settings.trace_correlation_ping_enabled:
        return
    k = ctx.kafka
    if k is None:
        return
    tid = str(trace or "").strip()
    if not tid:
        return
    body = build_suggest_remediation_body(
        tid,
        diagnosis=diagnosis,
        confidence=_clamp01(confidence),
        source=source,
        suggested_tool=suggested_tool,
    )
    try:
        await k.send_dict(ctx.settings.kafka_topic_actions, {"data": json.dumps(body, ensure_ascii=False)})
        logger.info(
            "event=action_emitted action=SUGGEST_REMEDIATION trace=%s source=%s",
            tid,
            source,
        )
    except Exception as e:
        logger.warning("action_emit skip: %s", e)


async def reason_from_diagnostic_evidence(ctx: WorkerHandlerContext, fields: dict[str, str]) -> str:
    """Evidence → batch → hard logic | RagGate | LLM; always emit SUGGEST_REMEDIATION when enabled."""
    raw = fields.get("data") or "{}"
    try:
        ev_doc = json.loads(raw)
    except Exception:
        ev_doc = {"kind": "parse_error", "raw": raw[:8000]}
    ev_doc = coerce_evidence_dict(ev_doc)
    trace = str(ev_doc.get("trace_id") or "evidence-unknown")
    tok = push_trace_id(trace)
    try:
        ctx.inbound_trace_id = trace
        rel = evidence_relevance_warning(
            str(ev_doc.get("alert_hint") or ""),
            str(ev_doc.get("probe") or ""),
        )
        if rel:
            logger.warning("event=evidence_relevance_mismatch detail=%s", rel[:500])

        batch = await append_evidence_and_take_flush_batch(ctx.redis, trace, ev_doc)
        if batch is None:
            return ""

        logger.info(
            "event=diag_batch_flush trace=%s probes=%s",
            trace,
            [x.get("probe") for x in batch],
        )

        chat_id: int | None = None
        ctx_blob = await ctx.redis.get(f"omni:evidence_reply:{trace}")
        if ctx_blob:
            try:
                meta = json.loads(ctx_blob.decode() if isinstance(ctx_blob, bytes) else ctx_blob)
                cid = meta.get("chat_id")
                if cid is not None:
                    chat_id = int(cid)
            except Exception:
                logger.warning("evidence_reply context parse failed")

        by_probe = {str(b.get("probe") or ""): dict(b) for b in batch}
        hard = apply_sdk_truth_hard_logic(by_probe)
        if hard is not None:
            await _emit_suggest_remediation(
                ctx,
                trace=trace,
                diagnosis=hard.strip(),
                confidence=0.95,
                source="HARD_LOGIC",
                suggested_tool="verify_metrics_alignment",
            )
            if chat_id is not None:
                pld = {
                    "trace_id": trace,
                    "source": "diagnostic_evidence",
                    "text": hard,
                    "diagnostic_evidence_sanitized": True,
                }
                await send_telegram_out_for_inbound(ctx, pld, trace, hard)
            return hard

        sanitized_text = format_batch_sanitized_analyst_user_text(batch)
        if len(batch) == 1:
            sanitized_text = format_sanitized_analyst_user_text(batch[0])

        ev_hints = _hints_from_evidence_text(sanitized_text)
        gate_out = await evaluate_rag_gate(ctx, sanitized_text, hints=ev_hints, trace=trace)
        if gate_out.hit and (gate_out.formatted or "").strip():
            diag_en = (gate_out.match_text_en or "").strip() or gate_out.formatted.strip()
            out = ope.truncate_plain_text_to_max_words(
                gate_out.formatted.strip(),
                max_words=int(getattr(ctx.settings, "omni_summary_max_words", 100)),
            )
            await _emit_suggest_remediation(
                ctx,
                trace=trace,
                diagnosis=diag_en,
                confidence=gate_out.best_score or 0.0,
                source="RAG_HIT",
                suggested_tool=gate_out.suggested_tool or "kubectl_describe_pod",
            )
            if chat_id is not None:
                pld = {
                    "trace_id": trace,
                    "source": "diagnostic_evidence",
                    "text": sanitized_text,
                    "diagnostic_evidence_sanitized": True,
                }
                await send_telegram_out_for_inbound(ctx, pld, trace, out)
            return out

        payload: dict[str, Any] = {
            "trace_id": trace,
            "source": "diagnostic_evidence",
            "text": sanitized_text,
            "diagnostic_evidence_sanitized": True,
            "batched_probes": [str(b.get("probe") or "") for b in batch],
            "rag_gate_evaluated": True,
        }
        if chat_id is not None:
            payload["chat_id"] = chat_id
        out = await reason_diagnostic_evidence_only(ctx, payload, trace)
        await _emit_suggest_remediation(
            ctx,
            trace=trace,
            diagnosis=(out or "").strip() or "Empty analyst output.",
            confidence=0.72,
            source="LLM_ANALYST",
            suggested_tool="inspect_pod_logs",
        )
        if chat_id is not None:
            await send_telegram_out_for_inbound(ctx, payload, trace, out)
        return out
    finally:
        pop_trace_id(tok)
