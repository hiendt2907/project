"""One structured record per LLM call — log + Prometheus + Tempo span.

Answers, for every call made to the model: *which trace, which model, what went
in, what came out, how long, how many tokens, did it succeed*.

Lives under ``src/pkg/`` so both the gateway and the worker images can import it
without crossing the gateway→workers boundary (INV: ``src/gateway/`` must not
import ``workers/``). Every integration point — Prometheus, OpenTelemetry, the
trace ContextVar — is a *guarded lazy import*: when the dependency is absent the
call degrades to a plain log line instead of raising.

INV_DATA_RESIDENCY
------------------
A prompt may embed customer log lines, hostnames, or config. This module
therefore **logs no prompt or response text by default**. What always ships is
non-reversible: character counts, token counts, and a truncated SHA-256 digest
(enough to prove "same prompt as last time" without carrying the content).

A bounded plaintext preview is opt-in via ``OMNI_LLM_LOG_PREVIEW_CHARS`` (0 =
off, hard-capped at ``MAX_PREVIEW_CHARS``) and is scrubbed for secret-shaped
tokens first. Turn it on in lab, leave it at 0 anywhere customer data lands.

Failure policy
--------------
Observability must never break a diagnosis, so every stage is wrapped. It is
*not* silent: failures log at WARNING with the reason, matching the project's
"no silently swallowed errors" rule.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any, Final

logger = logging.getLogger(__name__)

#: Absolute ceiling on any logged plaintext preview, regardless of env config.
MAX_PREVIEW_CHARS: Final[int] = 512

#: Digest length kept for prompt/response fingerprints (collision-safe enough to
#: correlate repeated prompts, too short to be a content channel).
_DIGEST_CHARS: Final[int] = 12

_ENV_PREVIEW_CHARS: Final[str] = "OMNI_LLM_LOG_PREVIEW_CHARS"
_ENV_ENABLED: Final[str] = "OMNI_LLM_OBSERVABILITY_ENABLED"

# Secret-shaped substrings scrubbed before any preview is emitted. Deliberately
# broad: a false positive costs a masked token, a false negative leaks a credential.
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)\b(?:api[_-]?key|secret|password|passwd|token|bearer|authorization)\b\s*[:=]\s*\S+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"),  # JWT
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),  # long base64 blob
)

_MASK: Final[str] = "[REDACTED]"


def observability_enabled() -> bool:
    """LLM observability on? Default **on** — it is the only view into the model."""
    return os.getenv(_ENV_ENABLED, "true").strip().lower() in ("1", "true", "yes")


def preview_chars() -> int:
    """Configured preview budget, clamped to ``[0, MAX_PREVIEW_CHARS]``.

    Default 0 (no plaintext) so the residency-safe path needs no configuration.
    """
    raw = os.getenv(_ENV_PREVIEW_CHARS, "0").strip()
    try:
        n = int(raw)
    except ValueError:
        logger.warning(
            "llm_observability: %s=%r is not an integer — preview disabled", _ENV_PREVIEW_CHARS, raw
        )
        return 0
    return max(0, min(MAX_PREVIEW_CHARS, n))


def digest(text: str | None) -> str:
    """Short, stable, non-reversible fingerprint of ``text`` (``""`` when empty)."""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:_DIGEST_CHARS]


def scrub_secrets(text: str) -> str:
    """Mask secret-shaped tokens. Applied before *any* plaintext leaves the process."""
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(_MASK, out)
    return out


def redact_for_log(text: str | None, budget: int | None = None) -> str:
    """Scrubbed, whitespace-collapsed, truncated preview honouring the budget.

    Returns ``""`` when previews are disabled — the caller then logs only
    lengths and digests, which is the INV_DATA_RESIDENCY-safe default.
    """
    cap = preview_chars() if budget is None else max(0, min(MAX_PREVIEW_CHARS, budget))
    if cap == 0 or not text:
        return ""
    collapsed = " ".join(text.split())
    scrubbed = scrub_secrets(collapsed)
    if len(scrubbed) <= cap:
        return scrubbed
    return f"{scrubbed[:cap]}…"


def messages_text(messages: list[dict[str, Any]] | None) -> str:
    """Flatten chat ``messages`` into the single string actually sent to the model."""
    if not messages:
        return ""
    parts: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif content is not None:
            parts.append(str(content))
    return "\n".join(parts)


def current_trace_id() -> str:
    """Active request trace id, or ``""``.

    Guarded: ``workers.request_trace`` is absent in the gateway image, and the
    ContextVar is unset outside a consumer loop. Both are normal, not errors.
    """
    try:
        from workers.request_trace import current_trace_id as _cur
    except ImportError:
        return ""
    try:
        tid = _cur()
    except Exception:  # noqa: BLE001 — a telemetry read must not raise into the caller
        return ""
    return "" if tid in ("", "unknown") else tid


def _emit_metrics(
    *,
    model: str,
    call_kind: str,
    outcome: str,
    prompt_chars: int,
    response_chars: int,
) -> None:
    """Best-effort Prometheus counters. ImportError is expected in the gateway."""
    try:
        from workers.metrics_exporter import observe_llm_call
    except ImportError:
        return
    try:
        observe_llm_call(
            model=model,
            call_kind=call_kind,
            outcome=outcome,
            prompt_chars=prompt_chars,
            response_chars=response_chars,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_observability: metric emit failed model=%s err=%s", model, exc)


def _trace_id_int(trace_id: str) -> int:
    """UUID-hex → 128-bit int, sha256 fallback.

    Mirrors ``workers.otel_tracing`` and ``otel_stage_span`` exactly so an
    ``llm.chat`` span lands inside the *same* Tempo trace as the pipeline stage
    spans for that diagnosis.
    """
    h = (trace_id or "").replace("-", "").lower()
    if len(h) != 32:
        h = hashlib.sha256((trace_id or "unknown").encode()).hexdigest()[:32]
    return int(h, 16)


def _emit_span(
    *,
    trace_id: str,
    model: str,
    call_kind: str,
    outcome: str,
    duration_ms: float,
    prompt_chars: int,
    response_chars: int,
    prompt_tokens: int,
    completion_tokens: int,
    error: str,
) -> None:
    """Emit ``llm.chat`` under the trace_id-derived trace. No-op without OTEL."""
    if not trace_id:
        return
    try:
        from opentelemetry import trace as _t
        from opentelemetry.trace import (
            NonRecordingSpan,
            SpanContext,
            TraceFlags,
            set_span_in_context,
        )
    except ImportError:
        return

    try:
        psid = int.from_bytes(
            hashlib.sha256(f"pipeline-root:{trace_id}".encode()).digest()[:8], "big"
        )
        psc = SpanContext(
            trace_id=_trace_id_int(trace_id),
            span_id=psid,
            is_remote=True,
            trace_flags=TraceFlags(0x01),
        )
        parent_ctx = set_span_in_context(NonRecordingSpan(psc))
        tracer = _t.get_tracer(__name__)
        with tracer.start_as_current_span("llm.chat", context=parent_ctx) as span:
            # Attribute names follow OTel GenAI semantic conventions where they exist.
            span.set_attribute("trace_id", trace_id)
            span.set_attribute("gen_ai.system", "ollama")
            span.set_attribute("gen_ai.request.model", model)
            span.set_attribute("llm.call_kind", call_kind)
            span.set_attribute("llm.outcome", outcome)
            span.set_attribute("llm.duration_ms", round(duration_ms, 2))
            span.set_attribute("llm.prompt_chars", prompt_chars)
            span.set_attribute("llm.response_chars", response_chars)
            span.set_attribute("gen_ai.usage.input_tokens", prompt_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", completion_tokens)
            if outcome != "ok":
                span.set_status(_t.Status(_t.StatusCode.ERROR, error or outcome))
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_observability: span emit failed model=%s err=%s", model, exc)


def record_llm_call(
    *,
    model: str,
    call_kind: str,
    prompt: str = "",
    response: str = "",
    duration_ms: float = 0.0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    outcome: str = "ok",
    error: str = "",
    trace_id: str | None = None,
    endpoint: str = "",
) -> None:
    """Record exactly one LLM call: structured log, Prometheus, Tempo span.

    ``prompt``/``response`` are consumed for *lengths and digests*; their text is
    logged only within the opt-in preview budget (see module docstring).

    Never raises — a broken telemetry path must not fail a diagnosis.
    """
    if not observability_enabled():
        return
    try:
        tid = trace_id if trace_id is not None else current_trace_id()
        p_chars, r_chars = len(prompt or ""), len(response or "")
        kind = call_kind or "unspecified"
        mdl = model or "unknown"

        fields: list[str] = [
            "event=llm_call",
            f"trace={tid or '-'}",
            f"model={mdl}",
            f"call_kind={kind}",
            f"outcome={outcome}",
            f"duration_ms={duration_ms:.1f}",
            f"prompt_chars={p_chars}",
            f"response_chars={r_chars}",
            f"prompt_tokens={prompt_tokens}",
            f"completion_tokens={completion_tokens}",
            f"prompt_sha={digest(prompt)}",
            f"response_sha={digest(response)}",
        ]
        if endpoint:
            fields.append(f"endpoint={endpoint}")
        if error:
            fields.append(f"error={redact_for_log(error, MAX_PREVIEW_CHARS)!r}")
        p_preview = redact_for_log(prompt)
        if p_preview:
            fields.append(f"prompt_preview={p_preview!r}")
        r_preview = redact_for_log(response)
        if r_preview:
            fields.append(f"response_preview={r_preview!r}")

        line = " ".join(fields)
        if outcome == "ok":
            logger.info(line)
        else:
            logger.warning(line)

        _emit_metrics(
            model=mdl,
            call_kind=kind,
            outcome=outcome,
            prompt_chars=p_chars,
            response_chars=r_chars,
        )
        _emit_span(
            trace_id=tid,
            model=mdl,
            call_kind=kind,
            outcome=outcome,
            duration_ms=duration_ms,
            prompt_chars=p_chars,
            response_chars=r_chars,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            error=error,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the hot path
        logger.warning("llm_observability: record_llm_call failed model=%s err=%s", model, exc)


def record_rag_gate(outcome: str, *, collection: str = "", trace_id: str | None = None) -> None:
    """Record a RAG gate decision — the "cache/KB hit vs real LLM call" signal.

    ``outcome`` mirrors ``RagGateOutcome``: ``hit``, ``miss``, ``cache_hit``,
    ``disabled``, ``query_too_short``, ``error``.
    """
    if not observability_enabled():
        return
    try:
        tid = trace_id if trace_id is not None else current_trace_id()
        logger.info(
            "event=rag_gate trace=%s outcome=%s collection=%s",
            tid or "-",
            outcome or "unknown",
            collection or "-",
        )
        try:
            from workers.metrics_exporter import inc_rag_gate_outcome
        except ImportError:
            return
        inc_rag_gate_outcome(outcome=outcome or "unknown", collection=collection or "unknown")
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_observability: record_rag_gate failed err=%s", exc)
