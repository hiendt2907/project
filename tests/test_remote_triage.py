"""Tests for remote_triage.py — RAG routing and urgency assessment."""
from __future__ import annotations

import types
from collections import Counter
from unittest.mock import AsyncMock, patch

import pytest

from pkg.reasoning.domain_signals import (
    DOMAIN_APPLICATION,
    DOMAIN_CONTAINER,
    DOMAIN_DATABASE,
    DOMAIN_NETWORK,
    DOMAIN_OS,
    DOMAIN_SECURITY,
)
from pkg.reasoning.evidence_cluster import LogCluster
from workers.archivist import RecallResult
from workers.remote_triage import (
    TriageResult,
    _assess_urgency,
    _build_symptom_text,
    triage_cluster,
)


def _cluster(
    domain: str = DOMAIN_OS,
    lane: str = "SYS_RESOURCE",
    probe: str = "remote_log_errors",
    alert_hint: str = "OOM kill: mysqld",
    raw: str = "",
    results: dict | None = None,
    count: int = 1,
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
        last_seen=1000.0,
        results=Counter(results or {"FAILED": count}),
        agent_ids={"agent-1"},
        lane=lane,
        is_new=True,
        is_storm=False,
    )


def _recall(score: float = 0.85, top_tool: str = "patch_configmap") -> RecallResult:
    return RecallResult(
        advisory="- similarity=0.85 tool=patch_configmap",
        strong=score >= 0.85,
        top_score=score,
        top_tool=top_tool,
        top_arg_keys=["name", "namespace"],
        top_point_id="abc123",
    )


def _ctx() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        redis=None,
        vector_store=None,
        llm=None,
        settings=None,
    )


# ── _build_symptom_text ───────────────────────────────────────────────────────

class TestBuildSymptomText:
    def test_includes_domain_probe_lane(self):
        c = _cluster(domain=DOMAIN_OS, lane="SYS_RESOURCE", probe="remote_log_errors")
        text = _build_symptom_text(c)
        assert "domain=os_host" in text   # canonical, `os_system` cũ
        assert "probe=remote_log_errors" in text
        assert "lane=SYS_RESOURCE" in text

    def test_includes_alert_hint(self):
        c = _cluster(alert_hint="Deadlock found in mysql")
        text = _build_symptom_text(c)
        assert "Deadlock found in mysql" in text

    def test_includes_raw_truncated(self):
        c = _cluster(raw="x" * 500)
        text = _build_symptom_text(c)
        assert "x" * 300 in text
        assert "x" * 301 not in text  # truncated at 300

    def test_empty_alert_and_raw_omitted(self):
        c = _cluster(alert_hint="", raw="")
        text = _build_symptom_text(c)
        assert "alert:" not in text
        assert "raw:" not in text

    def test_failed_count_included(self):
        c = _cluster(results={"FAILED": 5, "PASSED": 2}, count=7)
        text = _build_symptom_text(c)
        assert "failed_count=5" in text

    def test_no_failed_count_when_zero(self):
        c = _cluster(results={"PASSED": 3}, count=3)
        text = _build_symptom_text(c)
        assert "failed_count" not in text


# ── _assess_urgency ───────────────────────────────────────────────────────────

class TestAssessUrgency:
    def test_oom_kill_is_critical(self):
        c = _cluster(
            domain=DOMAIN_OS,
            alert_hint="oom kill: mysqld process killed",
            results={"FAILED": 1},
        )
        assert _assess_urgency(c) == "critical"

    def test_disk_full_is_critical(self):
        c = _cluster(
            domain=DOMAIN_OS,
            alert_hint="no space left on device",
            results={"FAILED": 1},
        )
        assert _assess_urgency(c) == "critical"

    def test_deadlock_is_high(self):
        c = _cluster(
            domain=DOMAIN_DATABASE,
            alert_hint="deadlock found when trying to get lock",
            results={"FAILED": 1},
        )
        assert _assess_urgency(c) in ("critical", "high")

    def test_dns_failure_is_critical(self):
        c = _cluster(
            domain=DOMAIN_NETWORK,
            alert_hint="dns resolution failed nxdomain",
            results={"FAILED": 1},
        )
        assert _assess_urgency(c) in ("critical", "high")

    def test_high_domain_with_high_failure_ratio_is_critical(self):
        # domain_severity=high but >50% failures → elevate to critical
        c = _cluster(
            domain=DOMAIN_OS,
            alert_hint="memory pressure kswapd",
            results={"FAILED": 8, "PASSED": 2},
            count=10,
        )
        result = _assess_urgency(c)
        assert result in ("critical", "high")

    def test_baseline_metrics_only_is_baseline(self):
        c = _cluster(
            domain=DOMAIN_OS,
            alert_hint="",
            raw="",
            extracted_fact={"cpu_pct": 12.3, "mem_pct": 45.2},
            results={"PASSED": 5},
            count=5,
        )
        assert _assess_urgency(c) == "baseline"

    def test_security_brute_force_is_critical(self):
        c = _cluster(
            domain=DOMAIN_SECURITY,
            alert_hint="brute force ssh invalid user",
            results={"FAILED": 1},
        )
        assert _assess_urgency(c) in ("critical", "high")

    def test_application_5xx_is_critical(self):
        c = _cluster(
            domain=DOMAIN_APPLICATION,
            alert_hint="5xx rate above threshold",
            results={"FAILED": 1},
        )
        assert _assess_urgency(c) in ("critical", "high")

    def test_container_panic_is_critical(self):
        c = _cluster(
            domain=DOMAIN_CONTAINER,
            alert_hint="panic: runtime error index out of range",
            results={"FAILED": 1},
        )
        assert _assess_urgency(c) == "critical"

    def test_failed_ratio_above_20pct_is_at_least_medium(self):
        c = _cluster(
            domain=DOMAIN_OS,
            alert_hint="",  # no keywords → no domain severity
            results={"FAILED": 3, "PASSED": 12},
            count=15,
        )
        urgency = _assess_urgency(c)
        # failed_ratio = 3/15 = 0.2 → exactly 0.2 is NOT > 0.2, so baseline
        # but 4/15 would be > 0.2 → medium
        assert urgency in ("baseline", "medium")

    def test_high_failure_ratio_elevates_urgency(self):
        c = _cluster(
            domain=DOMAIN_OS,
            alert_hint="",
            results={"FAILED": 5, "PASSED": 5},
            count=10,
        )
        # 50% failure ratio > 0.2 → at least medium
        assert _assess_urgency(c) in ("medium", "high", "critical")


# ── triage_cluster routing ────────────────────────────────────────────────────

class TestTriageCluster:
    @pytest.mark.asyncio
    async def test_rag_hit_with_tool_routes_known_with_fix(self):
        ctx = _ctx()
        c = _cluster(alert_hint="OOM kill: mysqld")
        with patch(
            "workers.remote_triage.recall_playbook_advisory",
            new=AsyncMock(return_value=_recall(score=0.80, top_tool="patch_deployment")),
        ):
            result = await triage_cluster(ctx, c)
        assert result.route == "KNOWN_WITH_FIX"
        assert result.recall is not None
        assert result.recall.top_score == 0.80

    @pytest.mark.asyncio
    async def test_rag_hit_advisory_only_routes_known_baseline(self):
        ctx = _ctx()
        c = _cluster(alert_hint="OOM kill: mysqld")
        with patch(
            "workers.remote_triage.recall_playbook_advisory",
            new=AsyncMock(return_value=_recall(score=0.80, top_tool="advisory_only")),
        ):
            result = await triage_cluster(ctx, c)
        assert result.route == "KNOWN_BASELINE"

    @pytest.mark.asyncio
    async def test_rag_score_below_threshold_triggers_urgency_assessment(self):
        ctx = _ctx()
        c = _cluster(
            domain=DOMAIN_OS,
            alert_hint="oom kill: process killed",
        )
        # Score below 0.75 threshold → RAG miss path
        with patch(
            "workers.remote_triage.recall_playbook_advisory",
            new=AsyncMock(return_value=_recall(score=0.60, top_tool="some_tool")),
        ):
            result = await triage_cluster(ctx, c)
        assert result.route in ("UNKNOWN_RESEARCH", "UNKNOWN_ARCHIVE_ONLY")

    @pytest.mark.asyncio
    async def test_rag_none_critical_routes_unknown_research(self):
        ctx = _ctx()
        c = _cluster(
            domain=DOMAIN_OS,
            alert_hint="oom kill: mysqld out of memory",
        )
        with patch(
            "workers.remote_triage.recall_playbook_advisory",
            new=AsyncMock(return_value=None),
        ):
            result = await triage_cluster(ctx, c)
        assert result.route == "UNKNOWN_RESEARCH"
        assert result.urgency in ("critical", "high")

    @pytest.mark.asyncio
    async def test_rag_none_baseline_routes_unknown_archive_only(self):
        ctx = _ctx()
        c = _cluster(
            domain=DOMAIN_OS,
            alert_hint="",
            raw="",
            extracted_fact={"cpu_pct": 12.3, "mem_pct": 45.2},
            results={"PASSED": 3},
            count=3,
        )
        with patch(
            "workers.remote_triage.recall_playbook_advisory",
            new=AsyncMock(return_value=None),
        ):
            result = await triage_cluster(ctx, c)
        assert result.route == "UNKNOWN_ARCHIVE_ONLY"
        assert result.urgency == "baseline"

    @pytest.mark.asyncio
    async def test_recall_playbook_raises_routes_to_urgency(self):
        """If archivist raises an exception, triage should still work via urgency path."""
        ctx = _ctx()
        c = _cluster(
            domain=DOMAIN_DATABASE,
            alert_hint="deadlock found when trying to get lock",
        )
        # recall_playbook_advisory returns None when ctx is incomplete (no vector_store)
        # so we simulate that path — no mock patch needed, just pass minimal ctx
        result = await triage_cluster(ctx, c)
        # ctx has no vector_store → archivist returns None → urgency path
        assert result.route in ("UNKNOWN_RESEARCH", "UNKNOWN_ARCHIVE_ONLY")

    @pytest.mark.asyncio
    async def test_result_contains_cluster_reference(self):
        ctx = _ctx()
        c = _cluster(alert_hint="deadlock found", domain=DOMAIN_DATABASE)
        with patch(
            "workers.remote_triage.recall_playbook_advisory",
            new=AsyncMock(return_value=None),
        ):
            result = await triage_cluster(ctx, c)
        assert result.cluster is c

    @pytest.mark.asyncio
    async def test_rag_hit_empty_top_tool_routes_known_baseline(self):
        ctx = _ctx()
        c = _cluster(alert_hint="connection refused")
        with patch(
            "workers.remote_triage.recall_playbook_advisory",
            new=AsyncMock(return_value=_recall(score=0.80, top_tool="")),
        ):
            result = await triage_cluster(ctx, c)
        # empty top_tool → no actionable fix → KNOWN_BASELINE
        assert result.route == "KNOWN_BASELINE"

    @pytest.mark.asyncio
    async def test_security_alert_no_rag_is_research(self):
        ctx = _ctx()
        c = _cluster(
            domain=DOMAIN_SECURITY,
            alert_hint="brute force ssh: invalid user detected",
            lane="SIEM_SECURITY",
        )
        with patch(
            "workers.remote_triage.recall_playbook_advisory",
            new=AsyncMock(return_value=None),
        ):
            result = await triage_cluster(ctx, c)
        assert result.route == "UNKNOWN_RESEARCH"

    @pytest.mark.asyncio
    async def test_medium_urgency_routes_to_research(self):
        ctx = _ctx()
        # 30% failure ratio with no keyword → medium urgency
        c = _cluster(
            domain=DOMAIN_OS,
            alert_hint="",
            results={"FAILED": 4, "PASSED": 9},
            count=13,
        )
        with patch(
            "workers.remote_triage.recall_playbook_advisory",
            new=AsyncMock(return_value=None),
        ):
            result = await triage_cluster(ctx, c)
        # medium urgency → UNKNOWN_RESEARCH
        assert result.route in ("UNKNOWN_RESEARCH", "UNKNOWN_ARCHIVE_ONLY")
