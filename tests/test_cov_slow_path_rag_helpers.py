"""Coverage tests for workers/slow_path_trace.py and pkg/rag/gate.py pure helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from workers.slow_path_trace import (
    AttemptRecord,
    _error_buckets_from_trace,
    _recommend_lines,
    build_slow_path_recovery_user_message,
    format_slow_path_autopsy,
    primary_bucket_for_metrics,
    slow_path_error_signature,
)


# ── slow_path_error_signature branches ───────────────────────────────────────

def _rec(attempt: int, phase: str, sig: str, one_line: str = "", tool: str | None = None) -> AttemptRecord:
    return AttemptRecord(attempt=attempt, phase=phase, error_signature=sig, one_line=one_line, tool=tool)


def test_error_sig_missing_pod():
    assert slow_path_error_signature("tool_error", "pod_name missing context") == "tool_error:missing_pod"


def test_error_sig_missing_pod_unicode():
    assert slow_path_error_signature("tool_error", "thiếu pod trong namespace") == "tool_error:missing_pod"


def test_error_sig_missing_namespace():
    assert slow_path_error_signature("tool_error", "missing namespace value") == "tool_error:missing_namespace"


def test_error_sig_k8s_api():
    assert slow_path_error_signature("tool_error", "ApiException from kubernetes 401") == "tool_error:k8s_api"


def test_error_sig_timeout():
    assert slow_path_error_signature("tool_error", "timeout exceeded") == "tool_error:timeout"


def test_error_sig_timed_out():
    assert slow_path_error_signature("tool_error", "operation timed out") == "tool_error:timeout"


def test_error_sig_network_connection():
    assert slow_path_error_signature("tool_error", "connection refused") == "tool_error:network"


def test_error_sig_unknown_phase():
    assert slow_path_error_signature("completely_unknown_phase", "") == "unknown_phase"


# ── build_slow_path_recovery_user_message branches ──────────────────────────

def test_build_recovery_empty_trace():
    """Line 81: early return on empty trace."""
    assert build_slow_path_recovery_user_message("goal", []) == ""


def test_build_recovery_with_prev_attempts():
    """Lines 89-93: summary of prior attempts with tool name."""
    trace = [
        AttemptRecord(1, "tool_error", "tool_error:timeout", "timed out", tool="kubectl_get_pods"),
        AttemptRecord(2, "parse", "parse_json", "bad json"),
    ]
    out = build_slow_path_recovery_user_message("restart crashed pod", trace)
    assert "Summary of prior attempts" in out
    assert "tool=kubectl_get_pods" in out
    assert "attempt 1" in out
    assert "Last attempt error" in out


def test_build_recovery_prev_attempt_no_tool():
    """Lines 89-93: summary with tool=None shows no tool= prefix."""
    trace = [
        AttemptRecord(1, "parse", "parse_json", "json error", tool=None),
        AttemptRecord(2, "empty_model", "empty_model", "empty"),
    ]
    out = build_slow_path_recovery_user_message("goal", trace)
    assert "attempt 1" in out
    assert "tool=" not in out.split("Summary")[1].split("Last attempt")[0]


def test_build_recovery_shell_allowed():
    """Line 99: shell_allowed branch."""
    trace = [AttemptRecord(1, "parse", "parse_json", "bad json")]
    out = build_slow_path_recovery_user_message("diagnose nginx", trace, shell_allowed=True)
    assert "execute_shell_command" in out


# ── _error_buckets_from_trace permission + missing_target branches ────────────

def test_error_buckets_permission_api():
    """Line 113: permission_api bucket."""
    trace = [_rec(1, "tool_error", "tool_error:permission")]
    buckets = _error_buckets_from_trace(trace)
    assert "permission_api" in buckets


def test_error_buckets_missing_target():
    """Line 115: missing_target bucket."""
    trace = [_rec(1, "tool_error", "tool_error:missing_pod")]
    buckets = _error_buckets_from_trace(trace)
    assert "missing_target" in buckets


def test_error_buckets_k8s_api():
    """Line 113: k8s_api also maps to permission_api."""
    trace = [_rec(1, "tool_error", "tool_error:k8s_api")]
    buckets = _error_buckets_from_trace(trace)
    assert "permission_api" in buckets


def test_error_buckets_missing_namespace():
    """Line 115: missing_namespace also maps to missing_target."""
    trace = [_rec(1, "tool_error", "tool_error:missing_namespace")]
    buckets = _error_buckets_from_trace(trace)
    assert "missing_target" in buckets


# ── _recommend_lines branches ─────────────────────────────────────────────────

def test_recommend_lines_permission_api():
    """Line 126: permission_api → RBAC recommendation."""
    recs = _recommend_lines(["permission_api"], [])
    assert any("RBAC" in r for r in recs)


def test_recommend_lines_hallucinated_tool():
    """Line 130: hallucinated_tool → ASCII name recommendation."""
    recs = _recommend_lines(["hallucinated_tool"], [])
    assert any("ASCII" in r for r in recs)


def test_recommend_lines_missing_target():
    recs = _recommend_lines(["missing_target"], [])
    assert any("namespace" in r.lower() or "pod" in r.lower() for r in recs)


def test_recommend_lines_empty_fallback():
    recs = _recommend_lines([], [])
    assert len(recs) >= 1


# ── primary_bucket_for_metrics mixed branch ───────────────────────────────────

def test_primary_bucket_mixed():
    """Line 201: multiple signatures → 'mixed'."""
    trace = [
        _rec(1, "parse", "parse_json"),
        _rec(2, "tool_error", "tool_error:timeout"),
    ]
    assert primary_bucket_for_metrics(trace) == "mixed"


def test_primary_bucket_single_dominant():
    """Single sig should not return mixed."""
    trace = [
        _rec(1, "parse", "parse_json"),
        _rec(2, "parse", "parse_json"),
    ]
    result = primary_bucket_for_metrics(trace)
    assert result == "parse_json"


# ── rag/gate.py pure helper branches ─────────────────────────────────────────

def test_clean_handles_empty_lines():
    """Line 57: empty stripped lines → continue (not kept)."""
    from pkg.rag import gate as rag_gate

    raw = "first line\n\n   \nsecond line"
    out = rag_gate.clean_and_truncate_context(raw, None)
    assert "first line" in out
    assert "second line" in out


def test_clean_normal_warning_error_pass_branch():
    """Line 67: line with 'normal', 'warning', 'error' hits pass then continues."""
    from pkg.rag import gate as rag_gate

    raw = "pod event normal warning error restart"
    out = rag_gate.clean_and_truncate_context(raw, None)
    # Should not be filtered — the pass branch lets it fall through
    assert out is not None


def test_incident_like_query_returns_false():
    """Line 119: query without incident keywords → False."""
    from pkg.rag.gate import _incident_like_query

    assert _incident_like_query("kubernetes deployment scaling strategy") is False
    assert _incident_like_query("what is a service account") is False


def test_incident_like_query_alert_context():
    """Line 118: [alert_context] keyword → True."""
    from pkg.rag.gate import _incident_like_query

    assert _incident_like_query("[alert_context] pod restart") is True


def test_post_filter_non_incident_returns_unchanged():
    """Line 150: non-incident query → return points unchanged."""
    from pkg.rag.gate import _post_filter_points_for_incident

    pt = MagicMock()
    pt.payload = {"metadata": {"type": "reference_guide"}}
    result = _post_filter_points_for_incident([pt], "kubernetes deployment strategy", enabled=True)
    assert result[0] is pt


def test_post_filter_disabled_returns_unchanged():
    """Line 147: enabled=False → return points immediately."""
    from pkg.rag.gate import _post_filter_points_for_incident

    pt = MagicMock()
    result = _post_filter_points_for_incident([pt], "CrashLoop error", enabled=False)
    assert result[0] is pt


# ── evaluate_rag_gate branches ────────────────────────────────────────────────

def _ws() -> SimpleNamespace:
    return SimpleNamespace(
        rag_gate_enabled=True,
        rag_gate_score_threshold=0.42,
        rag_gate_limit=4,
        rag_gate_query_max_chars=2000,
        rag_embed_max_tokens=512,
        rag_gate_chunk_max_chars=1200,
        rag_hybrid_search_enabled=False,
        rag_hot_cache_ttl_sec=3600,
        rag_hot_cache_enabled=False,
        rag_tier_uncertain_gate_enabled=False,
        rag_tier_knowledge_uncertain_threshold=0.7,
        rag_post_filter_metadata_enabled=False,
        embed_model="nomic-embed-text",
        embed_model_fallback=None,
        pgvector_collection_k8s_expert="k8s_expert",
        omni_concise_reply_max_words=500,
        omni_summary_max_words=1000,
    )


@pytest.mark.asyncio
async def test_evaluate_rag_gate_search_exception_llm_embed():
    """Lines 317-320: search exception with llm_embed phase."""
    from pkg.rag.gate import evaluate_rag_gate

    ws = _ws()
    mock_vs = AsyncMock()
    mock_vs.similarity_search = AsyncMock(side_effect=RuntimeError("rag_llm_embed_failed: model timeout"))
    ctx = SimpleNamespace(settings=ws, vector_store=mock_vs, llm=AsyncMock(), redis=None)
    out = await evaluate_rag_gate(ctx, "CrashLoop nginx pod restart error fail")
    assert out.hit is False
    assert out.detail.get("reason") == "search_error"
    assert out.detail.get("phase") == "llm_embed"


@pytest.mark.asyncio
async def test_evaluate_rag_gate_search_exception_pgvector():
    """Lines 317-320: search exception with pgvector phase."""
    from pkg.rag.gate import evaluate_rag_gate

    ws = _ws()
    mock_vs = AsyncMock()
    mock_vs.similarity_search = AsyncMock(side_effect=RuntimeError("rag_pgvector_query_failed: conn"))
    ctx = SimpleNamespace(settings=ws, vector_store=mock_vs, llm=AsyncMock(), redis=None)
    out = await evaluate_rag_gate(ctx, "CrashLoop nginx pod error timeout backoff")
    assert out.detail.get("phase") == "pgvector_query"


@pytest.mark.asyncio
async def test_evaluate_rag_gate_search_exception_unknown_phase():
    """Lines 317-320: unknown error → phase='unknown'."""
    from pkg.rag.gate import evaluate_rag_gate

    ws = _ws()
    mock_vs = AsyncMock()
    mock_vs.similarity_search = AsyncMock(side_effect=RuntimeError("some other error"))
    ctx = SimpleNamespace(settings=ws, vector_store=mock_vs, llm=AsyncMock(), redis=None)
    out = await evaluate_rag_gate(ctx, "CrashLoop nginx pod error timeout backoff")
    assert out.detail.get("phase") == "unknown"


@pytest.mark.asyncio
async def test_evaluate_rag_gate_empty_chunks():
    """Line 372: all chunks have empty text → empty_chunks."""
    from pkg.rag.gate import evaluate_rag_gate

    ws = _ws()
    pt = MagicMock()
    pt.score = 0.9
    pt.id = "c1"
    pt.payload = {"text": "", "summary": None, "metadata": {"type": "guide"}}

    mock_resp = MagicMock()
    mock_resp.points = [pt]
    mock_vs = AsyncMock()
    mock_vs.similarity_search = AsyncMock(return_value=mock_resp)
    ctx = SimpleNamespace(settings=ws, vector_store=mock_vs, llm=AsyncMock(), redis=None)
    out = await evaluate_rag_gate(ctx, "CrashLoop nginx pod error timeout backoff fail")
    assert out.hit is False
    assert out.detail.get("reason") == "empty_chunks"


@pytest.mark.asyncio
async def test_evaluate_rag_gate_hot_cache_get_exception_ignored():
    """Lines 282-285: redis.get raises → silently caught, proceeds to search."""
    from pkg.rag.gate import evaluate_rag_gate

    ws = _ws()
    ws.rag_hot_cache_enabled = True

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(side_effect=RuntimeError("redis down"))

    mock_resp = MagicMock()
    mock_resp.points = []
    mock_vs = AsyncMock()
    mock_vs.similarity_search = AsyncMock(return_value=mock_resp)
    ctx = SimpleNamespace(settings=ws, vector_store=mock_vs, llm=AsyncMock(), redis=mock_redis)
    out = await evaluate_rag_gate(ctx, "CrashLoop nginx error timeout backoff fail")
    assert out.hit is False


@pytest.mark.asyncio
async def test_evaluate_rag_gate_hot_cache_set_exception_ignored():
    """Lines 415-416: redis.setex raises → silently caught, result still returned."""
    from pkg.rag.gate import evaluate_rag_gate

    ws = _ws()
    ws.rag_hot_cache_enabled = True

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.setex = AsyncMock(side_effect=RuntimeError("redis write fail"))

    pt = MagicMock()
    pt.score = 0.9
    pt.id = "chunk-abc"
    pt.payload = {"text": "kubectl describe pod shows CrashLoop error", "metadata": {"type": "troubleshoot"}}

    mock_resp = MagicMock()
    mock_resp.points = [pt]
    mock_vs = AsyncMock()
    mock_vs.similarity_search = AsyncMock(return_value=mock_resp)
    ctx = SimpleNamespace(settings=ws, vector_store=mock_vs, llm=AsyncMock(), redis=mock_redis)
    out = await evaluate_rag_gate(ctx, "CrashLoop nginx error timeout backoff fail")
    assert out.hit is True
