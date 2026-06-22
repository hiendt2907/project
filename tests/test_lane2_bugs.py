"""Lane 2 (SYS_HARD_FAIL) production bug regression tests.

Each test targets a specific silent failure or wrong behavior under
real production conditions. No happy path tests.

Test report:
  L2-1: empty alert_ctx → RAG miss with no warning logged (degraded recall invisible)
  L2-2: terminal pair with empty root_cause → emits thin advisory, must return None instead
  L2-3: handler exception logged at DEBUG (invisible in prod), must be WARNING
  L2-4: os_state_validator probe exception logged at DEBUG, must be WARNING
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fakeredis.aioredis import FakeRedis

from rag.redis_vector_store import PointStruct, QueryResponse
from workers.os_diagnostic_loop import (
    _run_handler_or_generic,
    run_os_diagnostic_loop,
)


# ── L2-1 ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_os_loop_empty_alert_ctx_logs_warning(caplog):
    """L2-1: run_os_diagnostic_loop must log WARNING when alert_ctx has no alertname.

    When canonical_query_snippet is missing or non-JSON, _extract_alert_ctx returns {}.
    The RAG query becomes "alert= severity= ns= source=" — near-zero recall — and the
    loop exits silently at iteration 0 without any operator-visible indication.

    Before fix: loop exits at iter 0 (RAG miss), returns None, no log at WARNING level.
    After fix:  logs WARNING "os_diagnostic_loop: empty alert_ctx, RAG recall degraded trace=..."
                before entering the loop.
    """
    mock_vs = MagicMock()
    mock_vs.similarity_search = AsyncMock(return_value=QueryResponse(points=[]))

    ctx = SimpleNamespace(
        vector_store=mock_vs,
        llm=MagicMock(),
        settings=SimpleNamespace(embed_model="nomic-embed-text:latest"),
        redis=FakeRedis(decode_responses=True),
    )

    with caplog.at_level(logging.WARNING, logger="workers.os_diagnostic_loop"):
        result = await run_os_diagnostic_loop(
            ctx, batch=[], by_probe={}, alert_ctx={}, trace="trace-empty"
        )

    assert result is None

    # FAILS before fix (no warning logged), PASSES after fix
    assert any(
        "empty alert_ctx" in r.message
        for r in caplog.records
        if r.levelno >= logging.WARNING
    ), "Expected WARNING about empty alert_ctx, got none"


# ── L2-2 ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_os_loop_terminal_pair_empty_root_cause_returns_none(caplog):
    """L2-2: terminal pair with empty root_cause must return None, not emit thin advisory.

    When a RAG document is corrupt (missing root_cause/fix), _format_terminal_contrast
    produces a near-empty string:
      "[OS_DIAGNOSTIC_LOOP] terminal pair matched (domain=, confidence=0.00)."
    evidence_consumer then emits SUGGEST_REMEDIATION(source=OS_STATE_CONTRAST) with
    this useless string — operator receives an advisory with no actionable content.

    Before fix: returns the thin non-None contrast string.
    After fix:  logs WARNING "terminal pair missing root_cause, treating as miss"
                and returns None (falls through to LLM advisory).
    """
    bad_terminal = PointStruct(
        id="bad001",
        score=0.85,
        payload={
            "pair_type": "terminal",
            "root_cause": "",  # empty — the bug trigger
            "fix": "",
            "interpretation": "",
            "domain": "",
            "confidence": 0.0,
        },
    )
    mock_vs = MagicMock()
    mock_vs.similarity_search = AsyncMock(return_value=QueryResponse(points=[bad_terminal]))

    ctx = SimpleNamespace(
        vector_store=mock_vs,
        llm=MagicMock(),
        settings=SimpleNamespace(embed_model="nomic-embed-text:latest"),
        redis=FakeRedis(decode_responses=True),
    )
    alert_ctx = {
        "alertname": "DiskFull",
        "namespace": "prod",
        "source": "alertmanager",
        "labels": {"severity": "critical"},
        "annotations": {},
    }

    with caplog.at_level(logging.WARNING, logger="workers.os_diagnostic_loop"):
        result = await run_os_diagnostic_loop(
            ctx, batch=[], by_probe={}, alert_ctx=alert_ctx, trace="trace-bad-terminal"
        )

    # FAILS before fix (returns non-None thin contrast string), PASSES after fix
    assert result is None, (
        f"Expected None for terminal pair with empty root_cause, got: {result!r}"
    )
    assert any(
        "missing root_cause" in r.message
        for r in caplog.records
        if r.levelno >= logging.WARNING
    ), "Expected WARNING about missing root_cause, got none"


# ── L2-3 ─────────────────────────────────────────────────────────────────

def test_run_handler_exception_logs_warning_not_debug(caplog):
    """L2-3: _run_handler_or_generic must log WARNING (not DEBUG) when handler raises.

    Before fix: logger.debug("os_diagnostic_loop handler err=...") at line 61 —
                completely invisible at production INFO/WARNING log level.
    After fix:  logger.warning("os_diagnostic_loop: handler=... raised err=...").

    Side effect: None from a crashed handler is treated identically to a probe that
    found an anomaly in _build_sliding_window_q (both → "FAILED-or-anomaly" token).
    The WARNING makes crashed handlers identifiable in RAG query artifacts.
    """
    def crashing_handler(ev: dict, alert_ctx: dict) -> str | None:
        raise ValueError("simulated handler crash")

    with caplog.at_level(logging.WARNING, logger="workers.os_diagnostic_loop"):
        result = _run_handler_or_generic(crashing_handler, ev={}, alert_ctx={})

    assert result is None  # contract: still returns None

    # FAILS before fix (logged at DEBUG, not visible at WARNING level)
    # PASSES after fix (logged at WARNING)
    assert any(
        r.levelno >= logging.WARNING for r in caplog.records
    ), "Expected WARNING when handler raises, but only DEBUG was logged (invisible in prod)"


# ── L2-4 ─────────────────────────────────────────────────────────────────

def test_os_state_validator_probe_exception_logs_warning(caplog):
    """L2-4: compare_alert_claim_to_os_state must log WARNING when a probe handler raises.

    Before fix: logger.debug("os_state_validator handler=... err=...") at line 565 —
                invisible at INFO level, making silently crashing probes undetectable.
    After fix:  logger.warning("os_state_validator: handler=... raised err=...").

    In production with INFO-level logging, repeated probe crashes are completely
    invisible, making it impossible to diagnose why the state validator returns None.
    """
    from workers import os_state_validator
    from workers.os_state_validator import compare_alert_claim_to_os_state

    def crashing_probe(ev: dict, alert_ctx: dict) -> str | None:
        raise RuntimeError("probe infrastructure down")

    probe_name = "_test_crash_probe_l2_4"
    os_state_validator._OS_PROBE_HANDLERS[probe_name] = crashing_probe
    by_probe = {
        probe_name: {"probe": probe_name, "result": "FAILED", "extracted_fact": {}}
    }

    try:
        with caplog.at_level(logging.WARNING, logger="workers.os_state_validator"):
            compare_alert_claim_to_os_state(by_probe, alert_ctx={})
    finally:
        os_state_validator._OS_PROBE_HANDLERS.pop(probe_name, None)

    # FAILS before fix (DEBUG only, not visible at WARNING level)
    # PASSES after fix (WARNING logged)
    assert any(
        r.levelno >= logging.WARNING for r in caplog.records
    ), "Expected WARNING when probe handler raises, but only DEBUG was logged (invisible in prod)"
