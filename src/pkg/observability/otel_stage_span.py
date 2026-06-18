"""Emit one OpenTelemetry span per pipeline stage, linked by trace_id.

Lives under src/pkg/ so BOTH gateway and worker images can import it without
crossing the gateway→workers import boundary. The actual OTLP exporter +
TracerProvider is configured by the worker process (workers.otel_tracing); this
module only *emits* spans into whatever global provider is active. When the
OpenTelemetry SDK is missing or no real provider is installed (e.g. gateway
image, or OTEL disabled), every call is a cheap no-op.

Design: each pipeline stage transition (mark_stage) becomes a short span named
``stage.<STAGE>`` whose parent SpanContext is *derived deterministically from the
request trace_id* (W3C 128-bit, same scheme as workers.otel_tracing). This makes
every stage of one pipeline run group under a single Tempo trace whose id matches
the application trace_id — so "trace được luồng" is queryable by trace_id in
Grafana/Tempo without wrapping all 42 mark_stage call sites in a context manager.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

log = logging.getLogger(__name__)


def _trace_id_int_from_string(trace_id_str: str) -> int:
    """UUID-hex → 128-bit int; non-hex/short ids fall back to sha256. Mirrors
    workers.otel_tracing so worker-emitted root spans and stage spans share a trace."""
    h = (trace_id_str or "").replace("-", "").lower()
    if len(h) != 32:
        h = hashlib.sha256((trace_id_str or "unknown").encode()).hexdigest()[:32]
    return int(h, 16)


def emit_stage_span(
    trace_id: str,
    stage: str,
    status: str,
    *,
    detail: str = "",
    lane: str = "",
) -> None:
    """Emit a point-in-time span ``stage.<STAGE>`` under the trace_id-derived trace.

    Best-effort and fully guarded: no OpenTelemetry SDK, no active provider, or any
    runtime error → silent no-op. Never raises into the caller (mark_stage).
    """
    if not trace_id:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.trace import (
            NonRecordingSpan,
            SpanContext,
            TraceFlags,
            set_span_in_context,
        )
    except ImportError:
        return

    try:
        tid = _trace_id_int_from_string(trace_id)
        # Parent span id derived from trace_id so all stage spans share one parent
        # context (a stable virtual root for this pipeline run).
        psid = int.from_bytes(
            hashlib.sha256(f"pipeline-root:{trace_id}".encode()).digest()[:8], "big"
        )
        psc = SpanContext(
            trace_id=tid,
            span_id=psid,
            is_remote=True,
            trace_flags=TraceFlags(0x01),
        )
        parent_ctx = set_span_in_context(NonRecordingSpan(psc))
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span(f"stage.{stage}", context=parent_ctx) as span:
            try:
                span.set_attribute("trace_id", trace_id)
                span.set_attribute("pipeline.stage", str(stage))
                span.set_attribute("pipeline.status", str(status))
                if lane:
                    span.set_attribute("pipeline.lane", str(lane))
                if detail:
                    span.set_attribute("pipeline.detail", str(detail)[:256])
                if status == "fail":
                    span.set_status(trace.Status(trace.StatusCode.ERROR, detail or stage))
            except Exception:  # noqa: BLE001 — attribute set is best-effort
                pass
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the pipeline
        log.debug("otel_stage_span: emit failed stage=%s trace=%s err=%s", stage, trace_id, exc)
