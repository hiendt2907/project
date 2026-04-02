"""OpenTelemetry OTLP tracing — BatchSpanProcessor only; flush on shutdown."""

from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_tracer_provider: Any = None
_initialized: bool = False


def setup_otel_tracing(*, service_name: str, otlp_endpoint: str, enabled: bool) -> Any | None:
    """Configure TracerProvider + OTLP gRPC exporter + BatchSpanProcessor. Idempotent."""
    global _tracer_provider, _initialized
    if _initialized:
        return _tracer_provider
    _initialized = True
    if not enabled or not (otlp_endpoint or "").strip():
        logger.info("otel_tracing disabled (no endpoint or flag off)")
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    except ImportError as e:
        logger.warning("opentelemetry packages missing — otel disabled: %s", e)
        return None

    ep = otlp_endpoint.strip()
    if not ep.startswith("http"):
        ep = f"http://{ep}"
    resource = Resource.create({"service.name": service_name or "omni-worker"})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=ep, insecure=True)
    processor = BatchSpanProcessor(
        exporter,
        max_queue_size=2048,
        schedule_delay_millis=5000,
        max_export_batch_size=512,
        export_timeout_millis=30000,
    )
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    _tracer_provider = provider
    logger.info("otel_tracing enabled endpoint=%s service=%s", ep, service_name)
    return provider


def shutdown_otel_tracing() -> None:
    """Flush batches on worker exit (SIGTERM path)."""
    global _tracer_provider
    if _tracer_provider is None:
        return
    try:
        _tracer_provider.shutdown()
        logger.info("otel_tracing shutdown complete")
    except Exception as e:
        logger.debug("otel shutdown: %s", e)
    _tracer_provider = None


def _trace_id_int_from_string(trace_id_str: str) -> int:
    h = (trace_id_str or "").replace("-", "").lower()
    if len(h) != 32:
        h = hashlib.sha256((trace_id_str or "unknown").encode()).hexdigest()[:32]
    return int(h, 16)


@contextmanager
def inbound_trace_span(trace_id_str: str, name: str = "inbound_handler") -> Iterator[Any]:
    """Root span aligned with request trace_id (W3C 128-bit trace id from UUID hex)."""
    try:
        from opentelemetry import trace
        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, set_span_in_context
    except ImportError:
        yield None
        return

    tid = _trace_id_int_from_string(trace_id_str)
    sid = int.from_bytes(hashlib.sha256(f"root:{trace_id_str}".encode()).digest()[:8], "big")
    psc = SpanContext(
        trace_id=tid,
        span_id=sid,
        is_remote=True,
        trace_flags=TraceFlags(0x01),
    )
    parent_ctx = set_span_in_context(NonRecordingSpan(psc))
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span(name, context=parent_ctx) as span:
        try:
            span.set_attribute("trace_id", trace_id_str)
        except Exception:
            pass
        yield span


@contextmanager
def proactive_trace_span(trace_id_str: str, name: str = "proactive_event") -> Iterator[Any]:
    """Span for proactive_control_loop — same trace linkage as inbound_trace_span."""
    with inbound_trace_span(trace_id_str, name=name) as span:
        yield span


@contextmanager
def child_span(name: str, **attrs: Any) -> Iterator[Any]:
    """
    Create a nested span under current active span.
    No-op when OpenTelemetry SDK is unavailable.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        yield None
        return

    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span(name) as span:
        for k, v in attrs.items():
            if v is None:
                continue
            try:
                span.set_attribute(str(k), str(v)[:256])
            except Exception:
                continue
        yield span
