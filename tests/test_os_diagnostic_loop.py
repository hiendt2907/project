"""Tests for os_diagnostic_loop — iterative OS diagnostic loop (Lane 2)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fakeredis.aioredis import FakeRedis

from workers.os_diagnostic_loop import (
    _alert_ctx_summary,
    _build_sliding_window_q,
    _format_terminal_contrast,
    _run_handler_or_generic,
    run_os_diagnostic_loop,
)
from workers.evidence_consumer import _extract_alert_ctx
from rag.redis_vector_store import PointStruct, QueryResponse


# ── _build_sliding_window_q ───────────────────────────────────────────────

def test_build_sliding_window_q_empty_context():
    ctx = {"alertname": "DiskFull", "namespace": "prod", "source": "prometheus",
           "labels": {"severity": "critical"}, "annotations": {}}
    q = _build_sliding_window_q(ctx, [])
    assert "DiskFull" in q
    assert "ns=prod" in q
    assert len(q) <= 400


def test_build_sliding_window_q_with_steps():
    ctx = {"alertname": "MySQLDown", "namespace": "db", "source": "alertmanager",
           "labels": {}, "annotations": {}}
    # not-None finding → PASSED-no-anomaly; None → FAILED-or-anomaly (matches ingest format)
    stack = [("mysql_health", "some contrast text"), ("disk_usage", None)]
    q = _build_sliding_window_q(ctx, stack)
    assert "step1: probe=mysql_health result=PASSED-no-anomaly" in q
    assert "step2: probe=disk_usage result=FAILED-or-anomaly" in q
    assert len(q) <= 400


def test_build_sliding_window_q_normalized_format_matches_ingest():
    """Q format must match ingest script exactly: PASSED-no-anomaly / FAILED-or-anomaly."""
    ctx = {"alertname": "SystemdCriticalUnitFailed", "namespace": "infra",
           "source": "alertmanager", "labels": {"severity": "critical"}, "annotations": {}}
    stack = [("systemd_units", "any handler contrast string"), ("disk_usage", None)]
    q = _build_sliding_window_q(ctx, stack)
    assert "result=PASSED-no-anomaly" in q
    assert "result=FAILED-or-anomaly" in q
    # must NOT contain raw handler output in step lines
    assert "any handler contrast string" not in q


def test_build_sliding_window_q_truncated_at_400():
    ctx = {"alertname": "A" * 300, "namespace": "ns", "source": "src",
           "labels": {}, "annotations": {}}
    q = _build_sliding_window_q(ctx, [])
    assert len(q) <= 400


# ── _run_handler_or_generic ───────────────────────────────────────────────

def test_run_handler_registered_called():
    called = {}

    def fake_handler(ev, alert_ctx):
        called["ev"] = ev
        called["ctx"] = alert_ctx
        return "contrast string"

    ev = {"probe": "disk_usage", "result": "PASSED", "extracted_fact": "{}"}
    result = _run_handler_or_generic(fake_handler, ev, {"alertname": "test"})
    assert result == "contrast string"
    assert called["ctx"]["alertname"] == "test"


def test_run_handler_none_passed_no_anomaly():
    ev = {"probe": "custom_probe", "result": "PASSED",
          "extracted_fact": json.dumps({"healthy": True, "count": 5})}
    result = _run_handler_or_generic(None, ev, {})
    assert result is not None
    assert "PASSED" in result


def test_run_handler_none_passed_with_anomaly_key():
    ev = {"probe": "custom_probe", "result": "PASSED",
          "extracted_fact": json.dumps({"error_count": 3, "status": "ok"})}
    result = _run_handler_or_generic(None, ev, {})
    # "error_count" contains "error" — anomaly detected → descriptive string for context_stack
    assert result is not None
    assert "FAILED" in result or "anomaly" in result.lower()


def test_run_handler_none_failed():
    ev = {"probe": "custom_probe", "result": "FAILED",
          "extracted_fact": json.dumps({"status": "down"})}
    result = _run_handler_or_generic(None, ev, {})
    assert result is not None
    assert "FAILED" in result


def test_run_handler_exception_returns_none():
    def bad_handler(ev, ctx):
        raise RuntimeError("boom")

    ev = {"probe": "bad", "result": "PASSED", "extracted_fact": "{}"}
    result = _run_handler_or_generic(bad_handler, ev, {})
    assert result is None


# ── _format_terminal_contrast ─────────────────────────────────────────────

def test_format_terminal_contrast_full():
    pair = {
        "domain": "D0_systemd",
        "confidence": 0.92,
        "root_cause": "nginx.service crashed due to OOM",
        "fix": "restart nginx, increase memory limit",
        "interpretation": "alert is genuine",
    }
    ctx = {"alertname": "NginxDown", "namespace": "web", "labels": {}, "annotations": {}, "source": ""}
    result = _format_terminal_contrast(pair, ctx)
    assert "OS_DIAGNOSTIC_LOOP" in result
    assert "D0_systemd" in result
    assert "nginx.service" in result
    assert "restart nginx" in result
    assert "NginxDown" in result


def test_format_terminal_contrast_empty_pair():
    result = _format_terminal_contrast({}, {})
    assert "OS_DIAGNOSTIC_LOOP" in result


# ── run_os_diagnostic_loop ────────────────────────────────────────────────

def _make_ctx(search_response: QueryResponse | None = None, search_raises: bool = False):
    """Build a minimal WorkerHandlerContext-like SimpleNamespace for tests."""
    redis = FakeRedis(decode_responses=True)

    mock_vector_store = MagicMock()
    if search_raises:
        mock_vector_store.similarity_search = AsyncMock(side_effect=RuntimeError("rag down"))
    else:
        mock_vector_store.similarity_search = AsyncMock(
            return_value=search_response or QueryResponse(points=[])
        )

    settings = SimpleNamespace(embed_model="nomic-embed-text:latest")
    llm = MagicMock()

    return SimpleNamespace(
        redis=redis,
        vector_store=mock_vector_store,
        settings=settings,
        llm=llm,
    )


_ALERT_CTX = {
    "alertname": "DiskFull",
    "namespace": "prod",
    "source": "alertmanager",
    "labels": {"severity": "critical"},
    "annotations": {"summary": "Disk at 99%"},
}

_BY_PROBE = {
    "disk_usage": {
        "probe": "disk_usage",
        "result": "PASSED",
        "extracted_fact": json.dumps({"disk_critical_count": 0, "critical_partitions": [], "inode_critical": []}),
    }
}


@pytest.mark.asyncio
async def test_loop_rag_miss_returns_none():
    ctx = _make_ctx(QueryResponse(points=[]))
    result = await run_os_diagnostic_loop(ctx, [], _BY_PROBE, _ALERT_CTX, "trace-001")
    assert result is None


@pytest.mark.asyncio
async def test_loop_no_vector_store_returns_none():
    ctx = SimpleNamespace(
        vector_store=None,
        settings=SimpleNamespace(embed_model="nomic-embed-text:latest"),
        llm=MagicMock(),
        redis=FakeRedis(decode_responses=True),
    )
    result = await run_os_diagnostic_loop(ctx, [], _BY_PROBE, _ALERT_CTX, "trace-001")
    assert result is None


@pytest.mark.asyncio
async def test_loop_no_llm_returns_none():
    ctx = SimpleNamespace(
        vector_store=MagicMock(),
        settings=SimpleNamespace(embed_model="nomic-embed-text:latest"),
        llm=None,
        redis=FakeRedis(decode_responses=True),
    )
    result = await run_os_diagnostic_loop(ctx, [], _BY_PROBE, _ALERT_CTX, "trace-001")
    assert result is None


@pytest.mark.asyncio
async def test_loop_terminal_pair_returns_contrast():
    terminal_pair = PointStruct(
        id="abc123",
        score=0.92,
        payload={
            "pair_type": "terminal",
            "domain": "D2_storage",
            "confidence": 0.92,
            "root_cause": "disk mounted read-only after kernel panic",
            "fix": "remount rw or fsck",
            "interpretation": "genuine disk failure",
        },
    )
    ctx = _make_ctx(QueryResponse(points=[terminal_pair]))
    result = await run_os_diagnostic_loop(ctx, [], _BY_PROBE, _ALERT_CTX, "trace-terminal")
    assert result is not None
    assert "OS_DIAGNOSTIC_LOOP" in result
    assert "disk mounted read-only" in result
    assert "DiskFull" in result


@pytest.mark.asyncio
async def test_loop_entry_pair_then_rag_miss_returns_none():
    """entry pair suggests disk_usage probe → probe runs → next RAG misses → return None."""
    entry_pair = PointStruct(
        id="entry001",
        score=0.78,
        payload={
            "pair_type": "entry",
            "domain": "D2_storage",
            "next_check": "disk_usage",
        },
    )
    # First call: entry pair found; second call: miss
    ctx = _make_ctx()
    ctx.vector_store.similarity_search = AsyncMock(side_effect=[
        QueryResponse(points=[entry_pair]),
        QueryResponse(points=[]),
    ])
    result = await run_os_diagnostic_loop(ctx, [], _BY_PROBE, _ALERT_CTX, "trace-entry")
    assert result is None


@pytest.mark.asyncio
async def test_loop_entry_probe_missing_from_evidence_stops():
    """next_check probe not in by_probe → loop stops early → return None."""
    entry_pair = PointStruct(
        id="e1", score=0.80,
        payload={"pair_type": "entry", "next_check": "nonexistent_probe"},
    )
    ctx = _make_ctx(QueryResponse(points=[entry_pair]))
    result = await run_os_diagnostic_loop(ctx, [], {}, _ALERT_CTX, "trace-no-probe")
    assert result is None


@pytest.mark.asyncio
async def test_loop_rag_exception_returns_none():
    ctx = _make_ctx(search_raises=True)
    result = await run_os_diagnostic_loop(ctx, [], _BY_PROBE, _ALERT_CTX, "trace-exc")
    assert result is None


@pytest.mark.asyncio
async def test_loop_max_iterations_reached_returns_none():
    """All iterations return entry pairs without a terminal → exhaust max_iterations."""
    entry_pair = PointStruct(
        id="e1", score=0.80,
        payload={"pair_type": "entry", "next_check": "disk_usage"},
    )
    ctx = _make_ctx()
    ctx.vector_store.similarity_search = AsyncMock(return_value=QueryResponse(points=[entry_pair]))
    result = await run_os_diagnostic_loop(
        ctx, [], _BY_PROBE, _ALERT_CTX, "trace-max", max_iterations=3
    )
    assert result is None
    assert ctx.vector_store.similarity_search.call_count == 3


@pytest.mark.asyncio
async def test_loop_q_format_uses_normalized_tokens_not_raw_handler_output():
    """RAG query must use PASSED-no-anomaly/FAILED-or-anomaly, never raw handler output.

    If _build_sliding_window_q passes the actual contrast string (e.g. 'OS probe
    systemd_units reports all services healthy | ...') into the query, the vector
    will diverge from the ingest-time embeddings → recall ≈ 0 in production.
    """
    captured_queries: list[str] = []

    async def capturing_search(query, **kwargs):
        captured_queries.append(query)
        # return entry pair on first call to trigger a second iteration
        if len(captured_queries) == 1:
            return QueryResponse(points=[PointStruct(
                id="e1", score=0.80,
                payload={"pair_type": "entry", "next_check": "systemd_units"},
            )])
        return QueryResponse(points=[])

    ctx = _make_ctx()
    ctx.vector_store.similarity_search = capturing_search
    by_probe = {
        "systemd_units": {
            "probe": "systemd_units", "result": "PASSED",
            "extracted_fact": json.dumps({"critical_failed_units": [], "failed_units": []}),
        }
    }
    await run_os_diagnostic_loop(ctx, [], by_probe, _ALERT_CTX, "trace-qfmt")

    assert len(captured_queries) >= 2, "loop must have run a second iteration"
    second_q = captured_queries[1]
    # normalized token required
    assert "result=PASSED-no-anomaly" in second_q, (
        f"Q format mismatch — got: {second_q!r}"
    )
    # raw handler prose must NOT appear
    assert "reports all critical services healthy" not in second_q
    assert "OS probe" not in second_q


@pytest.mark.asyncio
async def test_loop_failed_probe_writes_failed_token_to_q():
    """When a probe FAILS, next iteration Q must contain FAILED-or-anomaly."""
    captured_queries: list[str] = []

    async def capturing_search(query, **kwargs):
        captured_queries.append(query)
        if len(captured_queries) == 1:
            return QueryResponse(points=[PointStruct(
                id="e1", score=0.80,
                payload={"pair_type": "entry", "next_check": "disk_usage"},
            )])
        return QueryResponse(points=[])

    ctx = _make_ctx()
    ctx.vector_store.similarity_search = capturing_search
    # disk_usage FAILED → handler returns None
    by_probe = {
        "disk_usage": {
            "probe": "disk_usage", "result": "FAILED",
            "extracted_fact": json.dumps({"disk_critical_count": 2, "critical_partitions": ["/data"]}),
        }
    }
    await run_os_diagnostic_loop(ctx, [], by_probe, _ALERT_CTX, "trace-failed-probe")

    assert len(captured_queries) >= 2
    second_q = captured_queries[1]
    assert "result=FAILED-or-anomaly" in second_q, (
        f"Failed probe must emit FAILED-or-anomaly token — got: {second_q!r}"
    )
    assert "result=no-result" not in second_q


@pytest.mark.asyncio
async def test_loop_sliding_window_capped_at_2():
    """After 2 steps in context, older entries are dropped from query."""
    calls: list[str] = []

    async def capture_search(query, **kwargs):
        calls.append(query)
        if len(calls) == 1:
            return QueryResponse(points=[PointStruct(
                id="e1", score=0.80,
                payload={"pair_type": "entry", "next_check": "disk_usage"},
            )])
        if len(calls) == 2:
            return QueryResponse(points=[PointStruct(
                id="e2", score=0.80,
                payload={"pair_type": "entry", "next_check": "disk_usage"},
            )])
        return QueryResponse(points=[])

    ctx = _make_ctx()
    ctx.vector_store.similarity_search = capture_search
    by_probe = {
        "disk_usage": {
            "probe": "disk_usage", "result": "PASSED",
            "extracted_fact": json.dumps({"disk_critical_count": 0, "critical_partitions": [], "inode_critical": []}),
        }
    }
    await run_os_diagnostic_loop(ctx, [], by_probe, _ALERT_CTX, "trace-window")
    # After iteration 2, the query should only have 2 step lines (sliding window=2)
    if len(calls) >= 3:
        lines = calls[2].split("\n")
        step_lines = [l for l in lines if l.startswith("step")]
        assert len(step_lines) <= 2


# ── _extract_alert_ctx ────────────────────────────────────────────────────

def test_extract_alert_ctx_from_alert_rule_and_namespace():
    batch = [{"alert_rule": "DiskFull", "namespace": "prod", "evidence_source": "RemoteAgent"}]
    ctx = _extract_alert_ctx(batch)
    assert ctx["alertname"] == "DiskFull"
    assert ctx["namespace"] == "prod"
    assert ctx["source"] == "RemoteAgent"


def test_extract_alert_ctx_from_canonical_query_snippet_json():
    snippet = json.dumps({
        "alertname": "MySQLDown",
        "namespace": "db",
        "labels": {"severity": "critical"},
        "annotations": {"summary": "MySQL primary unreachable"},
    })
    batch = [{"canonical_query_snippet": snippet, "evidence_source": "alertmanager"}]
    ctx = _extract_alert_ctx(batch)
    assert ctx["alertname"] == "MySQLDown"
    assert ctx["namespace"] == "db"
    assert ctx["labels"]["severity"] == "critical"
    assert ctx["annotations"]["summary"] == "MySQL primary unreachable"


def test_extract_alert_ctx_alert_rule_takes_priority_over_snippet():
    snippet = json.dumps({"alertname": "SnippetName"})
    batch = [{"alert_rule": "DirectName", "canonical_query_snippet": snippet}]
    ctx = _extract_alert_ctx(batch)
    assert ctx["alertname"] == "DirectName"


def test_extract_alert_ctx_empty_batch_returns_empty():
    assert _extract_alert_ctx([]) == {}


def test_extract_alert_ctx_malformed_snippet_is_ignored():
    batch = [{"alert_rule": "SomeAlert", "canonical_query_snippet": "not-json{{{"}]
    ctx = _extract_alert_ctx(batch)
    assert ctx["alertname"] == "SomeAlert"
    assert ctx["labels"] == {}
    assert ctx["annotations"] == {}
