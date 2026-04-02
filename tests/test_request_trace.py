"""request_trace helpers."""

from __future__ import annotations

from workers.request_trace import (
    current_trace_id,
    log_end_request,
    log_start_request,
    pop_trace_id,
    push_trace_id,
)


def test_push_pop_trace_id() -> None:
    assert current_trace_id() == "unknown"
    tok = push_trace_id("req-abc")
    assert current_trace_id() == "req-abc"
    pop_trace_id(tok)
    assert current_trace_id() == "unknown"


def test_log_start_end_no_crash() -> None:
    log_start_request("t1", phase="test", foo=1)
    log_end_request("t1", phase="test", status="ok", duration_ms=1.5, extra="x")
