"""Coverage for src/gateway/trace_context.py — no mocks."""

from __future__ import annotations

import logging

from gateway.trace_context import (
    OmniGatewayTraceFilter,
    current_gateway_trace_id,
    install_gateway_trace_logging,
    pop_gateway_trace_id,
    push_gateway_trace_id,
)


def test_push_pop_trace_roundtrip():
    assert current_gateway_trace_id() == ""
    tok = push_gateway_trace_id("trace-alpha-01")
    assert current_gateway_trace_id() == "trace-alpha-01"
    pop_gateway_trace_id(tok)
    assert current_gateway_trace_id() == ""


def test_filter_skips_when_trace_already_in_message():
    filt = OmniGatewayTraceFilter()
    rec = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="x",
        lineno=1,
        msg="hello [trace_id=tid-in-msg] world",
        args=(),
        exc_info=None,
    )
    assert filt.filter(rec) is True
    assert rec.msg == "hello [trace_id=tid-in-msg] world"


def test_filter_prefixes_message_with_trace_id():
    filt = OmniGatewayTraceFilter()
    tok = push_gateway_trace_id("gw-trace-xyz12")
    try:
        rec = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="x",
            lineno=1,
            msg="plain message",
            args=(),
            exc_info=None,
        )
        assert filt.filter(rec) is True
        assert "[trace_id=gw-trace-xyz12]" in rec.msg
        assert rec.args == ()
    finally:
        pop_gateway_trace_id(tok)


def test_install_gateway_trace_logging_idempotent():
    install_gateway_trace_logging()
    install_gateway_trace_logging()


def test_filter_getmessage_failure_is_safe():
    filt = OmniGatewayTraceFilter()
    tok = push_gateway_trace_id("safe-trace1")
    try:
        rec = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="x",
            lineno=1,
            msg="%(missing_key)s",
            args=(),
            exc_info=None,
        )
        assert filt.filter(rec) is True
    finally:
        pop_gateway_trace_id(tok)
