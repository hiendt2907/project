"""Tests for remote_advisor.py — evidence text building and LLM advisory flow."""
from __future__ import annotations

import types
from collections import Counter
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pkg.reasoning.domain_signals import (
    DOMAIN_APPLICATION,
    DOMAIN_CONTAINER,
    DOMAIN_DATABASE,
    DOMAIN_NETWORK,
    DOMAIN_OS,
    DOMAIN_SECURITY,
    DOMAIN_SERVICES,
    DOMAIN_STORAGE,
)
from pkg.reasoning.evidence_cluster import LogCluster
from workers.archivist import RecallResult
from workers.remote_advisor import _build_evidence_text, _DOMAIN_CONTEXT, analyze_cluster


def _cluster(
    domain: str = DOMAIN_OS,
    lane: str = "SYS_RESOURCE",
    probe: str = "remote_log_errors",
    alert_hint: str = "OOM kill: mysqld",
    raw: str = "",
    count: int = 3,
    results: dict | None = None,
    is_new: bool = False,
    is_storm: bool = False,
    extracted_fact: dict | None = None,
) -> LogCluster:
    return LogCluster(
        fingerprint=f"{probe}:abc123def456",
        probe=probe,
        domain=domain,
        representative={
            "probe": probe,
            "alert_hint": alert_hint,
            "raw": raw,
            "extracted_fact": extracted_fact or {},
            "result": "FAILED",
            "lane": lane,
        },
        count=count,
        first_seen=1000.0,
        last_seen=1010.0,
        results=Counter(results or {"FAILED": count}),
        agent_ids={"agent-1"},
        lane=lane,
        is_new=is_new,
        is_storm=is_storm,
    )


def _recall(score: float = 0.65, top_tool: str = "advisory_only") -> RecallResult:
    return RecallResult(
        advisory="- similarity=0.65 tool=advisory_only",
        strong=False,
        top_score=score,
        top_tool=top_tool,
        top_arg_keys=[],
        top_point_id="xyz",
    )


def _advisory_mock() -> MagicMock:
    m = MagicMock()
    m.verdict = "INVESTIGATE"
    m.confidence = "high"
    m.root_cause = "OOM kill triggered by mysqld exceeding memory limits"
    return m


# ── _build_evidence_text ──────────────────────────────────────────────────────

class TestBuildEvidenceText:
    def test_includes_domain_context_block(self):
        c = _cluster(domain=DOMAIN_OS)
        text = _build_evidence_text(c)
        assert "[DOMAIN: OS/SYSTEM]" in text

    def test_all_domains_have_context(self):
        domains = [
            DOMAIN_OS, DOMAIN_NETWORK, DOMAIN_STORAGE, DOMAIN_SERVICES,
            DOMAIN_CONTAINER, DOMAIN_DATABASE, DOMAIN_APPLICATION, DOMAIN_SECURITY,
        ]
        for d in domains:
            c = _cluster(domain=d)
            text = _build_evidence_text(c)
            assert "[DOMAIN:" in text, f"No domain context for {d}"

    def test_includes_probe_and_lane(self):
        c = _cluster(probe="mysql_status", lane="SYS_HARD_FAIL")
        text = _build_evidence_text(c)
        assert "probe=mysql_status" in text
        assert "lane=SYS_HARD_FAIL" in text

    def test_includes_occurrence_count(self):
        c = _cluster(count=7)
        text = _build_evidence_text(c)
        assert "occurrence_count=7" in text

    def test_includes_result_distribution(self):
        c = _cluster(results={"FAILED": 5, "PASSED": 2}, count=7)
        text = _build_evidence_text(c)
        assert "FAILED=5" in text
        assert "PASSED=2" in text

    def test_includes_alert_hint(self):
        c = _cluster(alert_hint="Deadlock found when trying to get lock")
        text = _build_evidence_text(c)
        assert "Deadlock found when trying to get lock" in text

    def test_raw_log_truncated_at_500(self):
        c = _cluster(raw="x" * 600)
        text = _build_evidence_text(c)
        assert "x" * 500 in text
        assert "x" * 501 not in text

    def test_empty_alert_hint_omitted(self):
        c = _cluster(alert_hint="", raw="")
        text = _build_evidence_text(c)
        assert "alert_hint:" not in text
        assert "raw_log:" not in text

    def test_extracted_metrics_included(self):
        c = _cluster(extracted_fact={"cpu_pct": 95.2, "mem_pct": 88.1})
        text = _build_evidence_text(c)
        assert "extracted_metrics:" in text
        assert "cpu_pct" in text

    def test_new_pattern_note_included(self):
        c = _cluster(is_new=True)
        text = _build_evidence_text(c)
        assert "First observation" in text

    def test_existing_pattern_no_new_note(self):
        c = _cluster(is_new=False)
        text = _build_evidence_text(c)
        assert "First observation" not in text

    def test_storm_warning_included(self):
        c = _cluster(is_storm=True, count=25)
        text = _build_evidence_text(c)
        assert "Log storm" in text

    def test_partial_rag_hint_included_when_recall_provided(self):
        c = _cluster()
        r = _recall(score=0.65)
        text = _build_evidence_text(c, recall=r)
        assert "PARTIAL RAG HINT" in text
        assert "score=0.650" in text
        assert "similarity=0.65" in text

    def test_no_rag_hint_when_recall_is_none(self):
        c = _cluster()
        text = _build_evidence_text(c, recall=None)
        assert "PARTIAL RAG HINT" not in text

    def test_database_domain_has_engine_guidance(self):
        c = _cluster(domain=DOMAIN_DATABASE)
        text = _build_evidence_text(c)
        assert "MySQL" in text or "database" in text.lower()


# ── analyze_cluster ───────────────────────────────────────────────────────────

class TestAnalyzeCluster:
    @pytest.mark.asyncio
    async def test_returns_advisory_on_success(self):
        ctx = types.SimpleNamespace()
        c = _cluster(alert_hint="OOM kill: mysqld")
        advisory = _advisory_mock()
        with patch(
            "workers.remote_advisor.run_advisory_analyst",
            new=AsyncMock(return_value=advisory),
        ):
            result = await analyze_cluster(ctx, c)
        assert result is advisory
        assert result.verdict == "INVESTIGATE"

    @pytest.mark.asyncio
    async def test_returns_none_on_llm_failure(self):
        ctx = types.SimpleNamespace()
        c = _cluster(alert_hint="OOM kill: mysqld")
        with patch(
            "workers.remote_advisor.run_advisory_analyst",
            new=AsyncMock(side_effect=RuntimeError("LLM timeout")),
        ):
            result = await analyze_cluster(ctx, c)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_llm_returns_none(self):
        ctx = types.SimpleNamespace()
        c = _cluster(alert_hint="OOM kill: mysqld")
        with patch(
            "workers.remote_advisor.run_advisory_analyst",
            new=AsyncMock(return_value=None),
        ):
            result = await analyze_cluster(ctx, c)
        assert result is None

    @pytest.mark.asyncio
    async def test_passes_correct_trace_as_fingerprint(self):
        ctx = types.SimpleNamespace()
        c = _cluster(probe="mysql_status")
        captured: list[dict] = []

        async def mock_advisory(ctx, payload, trace, evidence_text):
            captured.append({"trace": trace, "payload": payload})
            return _advisory_mock()

        with patch("workers.remote_advisor.run_advisory_analyst", new=mock_advisory):
            await analyze_cluster(ctx, c)

        assert captured[0]["trace"] == c.fingerprint

    @pytest.mark.asyncio
    async def test_payload_includes_domain_and_source(self):
        ctx = types.SimpleNamespace()
        c = _cluster(domain=DOMAIN_DATABASE, lane="SYS_HARD_FAIL", probe="mysql_status")
        captured: list[dict] = []

        async def mock_advisory(ctx, payload, trace, evidence_text):
            captured.append(payload)
            return None

        with patch("workers.remote_advisor.run_advisory_analyst", new=mock_advisory):
            await analyze_cluster(ctx, c)

        payload = captured[0]
        assert payload["evidence_source"] == "RemoteAgent"
        assert payload["domain"] == DOMAIN_DATABASE
        assert payload["lane"] == "SYS_HARD_FAIL"

    @pytest.mark.asyncio
    async def test_partial_recall_included_in_evidence(self):
        ctx = types.SimpleNamespace()
        c = _cluster(alert_hint="deadlock found")
        r = _recall(score=0.65)
        captured_texts: list[str] = []

        async def mock_advisory(ctx, payload, trace, evidence_text):
            captured_texts.append(evidence_text)
            return None

        with patch("workers.remote_advisor.run_advisory_analyst", new=mock_advisory):
            await analyze_cluster(ctx, c, recall=r)

        assert "PARTIAL RAG HINT" in captured_texts[0]
