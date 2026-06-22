"""OTEL stage span emission — verify mark_stage spans group under trace_id-derived trace."""
from __future__ import annotations

import pytest

from pkg.observability.otel_stage_span import _trace_id_int_from_string, emit_stage_span


def test_trace_id_derivation_stable_and_128bit() -> None:
    # Arrange
    tid = "1234567890abcdef1234567890abcdef"  # 32 hex chars
    # Act
    a = _trace_id_int_from_string(tid)
    b = _trace_id_int_from_string(tid)
    # Assert — deterministic + fits 128-bit
    assert a == b
    assert 0 < a < (1 << 128)


def test_non_hex_trace_id_falls_back_to_sha256() -> None:
    # Arrange — simulator ids look like "sim-..." (not 32-hex)
    a = _trace_id_int_from_string("sim-abc-123")
    b = _trace_id_int_from_string("sim-abc-123")
    assert a == b and 0 < a < (1 << 128)


def test_emit_stage_span_no_provider_is_noop() -> None:
    # Act / Assert — must never raise even with no TracerProvider configured
    emit_stage_span("sim-xyz", "RAG", "ok", detail="recall=0.81", lane="resource")
    emit_stage_span("", "RAG", "ok")  # empty trace → silent return


def test_emit_stage_span_exports_under_derived_trace() -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    # Arrange — install a real in-memory provider (overrides default NoOp once)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        trace.set_tracer_provider(provider)
    except Exception:
        pytest.skip("global tracer provider already set by another test")

    sim_trace = "sim-lane-resource-001"
    expected_tid = _trace_id_int_from_string(sim_trace)

    # Act — emit a couple of stage spans for the same pipeline trace
    emit_stage_span(sim_trace, "EVIDENCE", "ok", lane="resource")
    emit_stage_span(sim_trace, "CRAT", "fail", detail="audit_chain_write_failed", lane="resource")
    provider.force_flush()

    # Assert — both spans share the trace_id derived from the application trace_id
    spans = exporter.get_finished_spans()
    assert len(spans) >= 2
    names = {s.name for s in spans}
    assert "stage.EVIDENCE" in names and "stage.CRAT" in names
    for s in spans:
        assert s.context.trace_id == expected_tid
        assert s.attributes.get("trace_id") == sim_trace
    crat = next(s for s in spans if s.name == "stage.CRAT")
    assert crat.status.status_code.name == "ERROR"
