"""Tests for remote_diagnostic_archiver.py — RAG lesson writing."""
from __future__ import annotations

import types
from collections import Counter
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pkg.reasoning.domain_signals import DOMAIN_DATABASE, DOMAIN_OS
from pkg.reasoning.evidence_cluster import LogCluster
from rag.redis_vector_store import COLLECTION_ACTION_EXPERIENCE, COLLECTION_DIAGNOSTIC_HISTORY
from workers.archivist import RecallResult
from workers.remote_diagnostic_archiver import write_lessons
from workers.remote_triage import TriageResult


def _cluster(
    domain: str = DOMAIN_OS,
    lane: str = "SYS_RESOURCE",
    probe: str = "remote_log_errors",
    alert_hint: str = "OOM kill: mysqld",
    count: int = 3,
    results: dict | None = None,
    is_new: bool = True,
    is_storm: bool = False,
) -> LogCluster:
    return LogCluster(
        fingerprint=f"{probe}:abc123def456",
        probe=probe,
        domain=domain,
        representative={
            "probe": probe,
            "alert_hint": alert_hint,
            "raw": "",
            "extracted_fact": {},
            "result": "FAILED",
            "lane": lane,
            "trace_id": "trace-001",
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


def _triage(
    route: str = "UNKNOWN_RESEARCH",
    urgency: str = "critical",
    cluster: LogCluster | None = None,
) -> TriageResult:
    c = cluster or _cluster()
    return TriageResult(route=route, cluster=c, urgency=urgency, recall=None)


def _advisory(
    verdict: str = "INVESTIGATE",
    root_cause: str = "OOM kill triggered by mysqld",
    confidence: str = "high",
) -> MagicMock:
    m = MagicMock()
    m.verdict = verdict
    m.root_cause = root_cause
    m.confidence = confidence
    return m


def _ctx(upsert_calls: list | None = None) -> types.SimpleNamespace:
    captured: list[tuple] = [] if upsert_calls is None else upsert_calls

    async def mock_upsert(collection_name, points, *, ttl_sec=None):
        captured.append((collection_name, points))

    vs = MagicMock()
    vs.upsert = AsyncMock(side_effect=mock_upsert)

    llm = MagicMock()
    llm.embed = AsyncMock(return_value={"embedding": [0.1] * 768})

    settings = MagicMock()
    settings.embed_model = "nomic-embed-text"

    return types.SimpleNamespace(
        vector_store=vs,
        llm=llm,
        settings=settings,
        _upsert_calls=captured,
    )


# ── No vector_store guard ─────────────────────────────────────────────────────

class TestNoVectorStore:
    @pytest.mark.asyncio
    async def test_write_lessons_skips_when_no_vector_store(self):
        ctx = types.SimpleNamespace(vector_store=None)
        c = _cluster()
        t = _triage(cluster=c)
        # Should not raise
        await write_lessons(ctx, c, t, advisory=None)

    @pytest.mark.asyncio
    async def test_write_lessons_skips_when_no_llm(self):
        vs = MagicMock()
        vs.upsert = AsyncMock()
        ctx = types.SimpleNamespace(vector_store=vs, llm=None, settings=None)
        c = _cluster()
        t = _triage(cluster=c)
        await write_lessons(ctx, c, t, advisory=None)
        vs.upsert.assert_not_called()


# ── History write (always) ────────────────────────────────────────────────────

class TestHistoryWrite:
    @pytest.mark.asyncio
    async def test_always_writes_to_diagnostic_history(self):
        upsert_calls: list = []
        ctx = _ctx(upsert_calls)
        c = _cluster()
        t = _triage(cluster=c)
        await write_lessons(ctx, c, t, advisory=None)
        collections = [call[0] for call in upsert_calls]
        assert COLLECTION_DIAGNOSTIC_HISTORY in collections

    @pytest.mark.asyncio
    async def test_baseline_cluster_still_written(self):
        upsert_calls: list = []
        ctx = _ctx(upsert_calls)
        c = _cluster(alert_hint="", results={"PASSED": 5}, count=5)
        t = _triage(route="UNKNOWN_ARCHIVE_ONLY", urgency="baseline", cluster=c)
        await write_lessons(ctx, c, t, advisory=None)
        collections = [call[0] for call in upsert_calls]
        assert COLLECTION_DIAGNOSTIC_HISTORY in collections

    @pytest.mark.asyncio
    async def test_history_payload_contains_required_fields(self):
        upsert_calls: list = []
        ctx = _ctx(upsert_calls)
        c = _cluster(domain=DOMAIN_DATABASE, lane="SYS_HARD_FAIL", count=7)
        t = _triage(urgency="high", cluster=c)
        await write_lessons(ctx, c, t, advisory=None)
        hist_calls = [call for call in upsert_calls if call[0] == COLLECTION_DIAGNOSTIC_HISTORY]
        assert hist_calls
        payload = hist_calls[0][1][0].payload
        assert payload["fingerprint"] == c.fingerprint
        assert payload["domain"] == DOMAIN_DATABASE
        assert payload["lane"] == "SYS_HARD_FAIL"
        assert payload["count"] == 7
        assert payload["urgency"] == "high"
        assert payload["is_new_pattern"] is True
        assert "ts" in payload

    @pytest.mark.asyncio
    async def test_history_payload_includes_advisory_verdict_when_present(self):
        upsert_calls: list = []
        ctx = _ctx(upsert_calls)
        c = _cluster()
        t = _triage(cluster=c)
        adv = _advisory(verdict="CRITICAL")
        await write_lessons(ctx, c, t, advisory=adv)
        hist_calls = [call for call in upsert_calls if call[0] == COLLECTION_DIAGNOSTIC_HISTORY]
        payload = hist_calls[0][1][0].payload
        assert payload["advisory_verdict"] == "CRITICAL"

    @pytest.mark.asyncio
    async def test_history_payload_advisory_verdict_none_when_no_advisory(self):
        upsert_calls: list = []
        ctx = _ctx(upsert_calls)
        c = _cluster()
        t = _triage(cluster=c)
        await write_lessons(ctx, c, t, advisory=None)
        hist_calls = [call for call in upsert_calls if call[0] == COLLECTION_DIAGNOSTIC_HISTORY]
        payload = hist_calls[0][1][0].payload
        assert payload["advisory_verdict"] is None


# ── Experience write (advisory only) ─────────────────────────────────────────

class TestExperienceWrite:
    @pytest.mark.asyncio
    async def test_no_experience_write_without_advisory(self):
        upsert_calls: list = []
        ctx = _ctx(upsert_calls)
        c = _cluster()
        t = _triage(cluster=c)
        await write_lessons(ctx, c, t, advisory=None)
        collections = [call[0] for call in upsert_calls]
        assert COLLECTION_ACTION_EXPERIENCE not in collections

    @pytest.mark.asyncio
    async def test_experience_write_with_advisory(self):
        upsert_calls: list = []
        ctx = _ctx(upsert_calls)
        c = _cluster()
        t = _triage(cluster=c)
        adv = _advisory()
        await write_lessons(ctx, c, t, advisory=adv)
        collections = [call[0] for call in upsert_calls]
        assert COLLECTION_ACTION_EXPERIENCE in collections

    @pytest.mark.asyncio
    async def test_experience_payload_has_remote_diagnostic_kind(self):
        upsert_calls: list = []
        ctx = _ctx(upsert_calls)
        c = _cluster()
        t = _triage(cluster=c)
        adv = _advisory()
        await write_lessons(ctx, c, t, advisory=adv)
        exp_calls = [call for call in upsert_calls if call[0] == COLLECTION_ACTION_EXPERIENCE]
        payload = exp_calls[0][1][0].payload
        assert payload["memory_kind"] == "remote_diagnostic"

    @pytest.mark.asyncio
    async def test_experience_payload_contains_advisory_fields(self):
        upsert_calls: list = []
        ctx = _ctx(upsert_calls)
        c = _cluster(domain=DOMAIN_DATABASE)
        t = _triage(cluster=c)
        adv = _advisory(verdict="INVESTIGATE", root_cause="Deadlock in InnoDB", confidence="high")
        await write_lessons(ctx, c, t, advisory=adv)
        exp_calls = [call for call in upsert_calls if call[0] == COLLECTION_ACTION_EXPERIENCE]
        payload = exp_calls[0][1][0].payload
        assert payload["advisory_verdict"] == "INVESTIGATE"
        assert payload["advisory_root_cause"] == "Deadlock in InnoDB"
        assert payload["advisory_confidence"] == "high"
        assert payload["domain"] == DOMAIN_DATABASE
        assert payload["fingerprint"] == c.fingerprint
        assert payload["occurrence_count"] == c.count
        assert payload["exec_outcome"] == "advisory_only"

    @pytest.mark.asyncio
    async def test_both_writes_share_same_embedding(self):
        """Both COLLECTION_DIAGNOSTIC_HISTORY and COLLECTION_ACTION_EXPERIENCE use the same vector."""
        upsert_calls: list = []
        ctx = _ctx(upsert_calls)
        c = _cluster()
        t = _triage(cluster=c)
        adv = _advisory()
        await write_lessons(ctx, c, t, advisory=adv)
        # llm.embed should be called exactly once (vector reused for both collections)
        assert ctx.llm.embed.call_count == 1
        # Both collections written
        assert len(upsert_calls) == 2

    @pytest.mark.asyncio
    async def test_embed_failure_skips_all_writes(self):
        ctx = _ctx()
        ctx.llm.embed = AsyncMock(side_effect=RuntimeError("embed timeout"))
        c = _cluster()
        t = _triage(cluster=c)
        await write_lessons(ctx, c, t, advisory=_advisory())
        ctx.vector_store.upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_history_upsert_failure_does_not_raise(self):
        """Best-effort — upsert failure should not propagate."""
        ctx = _ctx()
        ctx.vector_store.upsert = AsyncMock(side_effect=RuntimeError("redis down"))
        c = _cluster()
        t = _triage(cluster=c)
        # Should not raise
        await write_lessons(ctx, c, t, advisory=_advisory())

    @pytest.mark.asyncio
    async def test_storm_cluster_still_archived(self):
        upsert_calls: list = []
        ctx = _ctx(upsert_calls)
        c = _cluster(is_storm=True, count=25)
        t = _triage(route="UNKNOWN_RESEARCH", urgency="critical", cluster=c)
        await write_lessons(ctx, c, t, advisory=_advisory())
        collections = [call[0] for call in upsert_calls]
        assert COLLECTION_DIAGNOSTIC_HISTORY in collections
