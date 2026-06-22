"""Coverage tests for remote_agent_pipeline.py — stages 2-6."""
from __future__ import annotations

import time
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pkg.reasoning.domain_signals import DOMAIN_CONTAINER, DOMAIN_OS, DOMAIN_UNKNOWN
from pkg.reasoning.evidence_cluster import LogCluster
from workers.remote_agent_pipeline import handle_remote_agent_evidence
from workers.remote_triage import TriageResult


def _cluster(domain: str = DOMAIN_OS, route: str = "KNOWN_PATTERN", urgency: str = "baseline") -> LogCluster:
    return LogCluster(
        fingerprint="abc:def123",
        probe="remote_system_metrics",
        domain=domain,
        representative={"alert_hint": "OOM kill"},
        count=1,
        first_seen=time.time(),
        last_seen=time.time(),
        results=Counter({"FAILED": 1}),
        agent_ids={"agent-1"},
        lane="SYS_RESOURCE",
        is_new=True,
        is_storm=False,
    )


def _ctx(chat_id: int | None = None) -> SimpleNamespace:
    from fakeredis.aioredis import FakeRedis
    redis = FakeRedis(decode_responses=True)
    settings = SimpleNamespace(
        telegram_admin_chat_id=chat_id,
        omni_trace_memory_tool_output_max_chars=4000,
    )
    return SimpleNamespace(
        redis=redis,
        kafka=None,
        settings=settings,
        llm=AsyncMock(),
        telegram_chat_id=chat_id,
    )


@pytest.fixture
def ev_doc():
    return {
        "evidence_source": "RemoteAgent",
        "probe": "remote_system_metrics",
        "alert_hint": "High CPU usage",
        "raw": "cpu=95%",
        "lane": "SYS_RESOURCE",
        "alert_rule": "HighCPUAlert",
        "namespace": "multi-agent",
        "extracted_fact": {"agent_id": "agent-1"},
    }


@pytest.mark.asyncio
async def test_cluster_upsert_fail_returns_empty(ev_doc):
    ctx = _ctx()
    with patch("workers.remote_agent_pipeline.upsert_cluster", side_effect=RuntimeError("redis down")):
        result = await handle_remote_agent_evidence(ctx, ev_doc, "trace-001")
    assert result == ""


@pytest.mark.asyncio
async def test_known_pattern_no_llm(ev_doc):
    ctx = _ctx()
    cluster = _cluster(route="KNOWN_PATTERN", urgency="baseline")
    triage = TriageResult(route="KNOWN_PATTERN", cluster=cluster, urgency="baseline")
    with (
        patch("workers.remote_agent_pipeline.upsert_cluster", return_value=cluster),
        patch("workers.remote_agent_pipeline.triage_cluster", return_value=triage),
        patch("workers.remote_agent_pipeline.write_lessons", new_callable=AsyncMock),
        patch("workers.remote_agent_pipeline.analyze_cluster") as mock_llm,
    ):
        result = await handle_remote_agent_evidence(ctx, ev_doc, "trace-002")
    mock_llm.assert_not_called()
    assert "KNOWN_PATTERN" in result
    assert "no_advisory" in result


@pytest.mark.asyncio
async def test_unknown_research_calls_llm(ev_doc):
    ctx = _ctx()
    cluster = _cluster()
    triage = TriageResult(route="UNKNOWN_RESEARCH", cluster=cluster, urgency="baseline")
    advisory = MagicMock(verdict="INVESTIGATE", trace_id="trace-003")
    with (
        patch("workers.remote_agent_pipeline.upsert_cluster", return_value=cluster),
        patch("workers.remote_agent_pipeline.triage_cluster", return_value=triage),
        patch("workers.remote_agent_pipeline.analyze_cluster", return_value=advisory),
        patch("workers.remote_agent_pipeline.write_lessons", new_callable=AsyncMock),
    ):
        result = await handle_remote_agent_evidence(ctx, ev_doc, "trace-003")
    assert "INVESTIGATE" in result


@pytest.mark.asyncio
async def test_critical_urgency_triggers_diagnosis_loop(ev_doc):
    """UNKNOWN_RESEARCH + critical urgency + LLM available → background diagnosis loop launched."""
    ctx = _ctx(chat_id=12345)
    cluster = _cluster()
    triage = TriageResult(route="UNKNOWN_RESEARCH", cluster=cluster, urgency="critical")
    with (
        patch("workers.remote_agent_pipeline.upsert_cluster", return_value=cluster),
        patch("workers.remote_agent_pipeline.triage_cluster", return_value=triage),
        patch("workers.remote_agent_pipeline.write_lessons", new_callable=AsyncMock),
        patch("workers.remote_agent_pipeline.asyncio.create_task") as mock_task,
    ):
        result = await handle_remote_agent_evidence(ctx, ev_doc, "trace-004")
    # Background task must have been created for multi-turn loop
    mock_task.assert_called_once()
    assert "diagnosis_loop_launched" in result


@pytest.mark.asyncio
async def test_high_urgency_launches_diagnosis_loop(ev_doc):
    """UNKNOWN_RESEARCH + high urgency + LLM available → background diagnosis loop launched."""
    ctx = _ctx(chat_id=12345)
    cluster = _cluster()
    triage = TriageResult(route="UNKNOWN_RESEARCH", cluster=cluster, urgency="high")
    with (
        patch("workers.remote_agent_pipeline.upsert_cluster", return_value=cluster),
        patch("workers.remote_agent_pipeline.triage_cluster", return_value=triage),
        patch("workers.remote_agent_pipeline.write_lessons", new_callable=AsyncMock),
        patch("workers.remote_agent_pipeline.asyncio.create_task") as mock_task,
    ):
        result = await handle_remote_agent_evidence(ctx, ev_doc, "trace-005")
    mock_task.assert_called_once()
    assert "diagnosis_loop_launched" in result


@pytest.mark.asyncio
async def test_labels_built_from_ev_doc(ev_doc):
    ctx = _ctx()
    cluster = _cluster(domain=DOMAIN_CONTAINER)
    triage = TriageResult(route="KNOWN_PATTERN", cluster=cluster, urgency="baseline")
    with (
        patch("workers.remote_agent_pipeline.upsert_cluster", return_value=cluster),
        patch("workers.remote_agent_pipeline.triage_cluster", return_value=triage),
        patch("workers.remote_agent_pipeline.write_lessons", new_callable=AsyncMock),
        patch("workers.remote_agent_pipeline.detect_domain", return_value=DOMAIN_CONTAINER) as mock_dd,
    ):
        await handle_remote_agent_evidence(ctx, ev_doc, "trace-006")
    call_kwargs = mock_dd.call_args
    assert call_kwargs.kwargs.get("labels") is not None
    assert call_kwargs.kwargs["labels"]["alertname"] == "HighCPUAlert"
    assert call_kwargs.kwargs["labels"]["namespace"] == "multi-agent"


@pytest.mark.asyncio
async def test_missing_lane_passes_empty_string(ev_doc):
    ev_doc.pop("lane")
    ctx = _ctx()
    cluster = _cluster()
    triage = TriageResult(route="KNOWN_PATTERN", cluster=cluster, urgency="baseline")
    with (
        patch("workers.remote_agent_pipeline.upsert_cluster", return_value=cluster),
        patch("workers.remote_agent_pipeline.triage_cluster", return_value=triage),
        patch("workers.remote_agent_pipeline.write_lessons", new_callable=AsyncMock),
        patch("workers.remote_agent_pipeline.detect_domain", return_value=DOMAIN_UNKNOWN) as mock_dd,
    ):
        await handle_remote_agent_evidence(ctx, ev_doc, "trace-007")
    assert mock_dd.call_args.args[3] == ""


@pytest.mark.asyncio
async def test_extracted_fact_as_string_resolves_agent_id():
    """Bug fix: coerce_evidence_dict serializes extracted_fact to JSON string.
    Pipeline must deserialize it to recover agent_id (not fall back to unknown-agent).
    """
    import json as _json
    ev = {
        "evidence_source": "RemoteAgent",
        "probe": "remote_log_errors",
        "alert_hint": "disk full",
        "raw": "disk full /var",
        "lane": "APP_LOG",
        "alert_rule": "DiskFull",
        "namespace": "loyalty-uat",
        # extracted_fact as JSON string (as produced by coerce_evidence_dict)
        "extracted_fact": _json.dumps({"agent_id": "loyalty-uat", "hostname": "10.210.14.86"}),
    }
    ctx = _ctx()
    cluster = _cluster()
    triage = TriageResult(route="KNOWN_PATTERN", cluster=cluster, urgency="baseline")
    captured_agent_id: list[str] = []

    async def _fake_upsert(redis, agent_id, fp, ev_doc, domain):
        captured_agent_id.append(agent_id)
        return cluster

    with (
        patch("workers.remote_agent_pipeline.upsert_cluster", side_effect=_fake_upsert),
        patch("workers.remote_agent_pipeline.triage_cluster", return_value=triage),
        patch("workers.remote_agent_pipeline.write_lessons", new_callable=AsyncMock),
    ):
        await handle_remote_agent_evidence(ctx, ev, "trace-bugfix-1")

    assert captured_agent_id == ["loyalty-uat"], (
        f"agent_id resolved to {captured_agent_id!r} — expected 'loyalty-uat'. "
        "extracted_fact JSON string was not deserialized."
    )


@pytest.mark.asyncio
async def test_coerce_evidence_dict_preserves_lane():
    """Bug fix: lane field was missing from coerce_evidence_dict allowlist — got dropped."""
    from pkg.reasoning.schema import coerce_evidence_dict
    raw = {
        "trace_id": "t-001",
        "probe": "remote_log_errors",
        "lane": "APP_LOG",
        "evidence_source": "RemoteAgent",
        "alert_hint": "disk full",
        "raw": "some error",
        "extracted_fact": {"agent_id": "loyalty-uat"},
    }
    result = coerce_evidence_dict(raw)
    assert result.get("lane") == "APP_LOG", (
        f"lane={result.get('lane')!r} — should be 'APP_LOG'. "
        "lane is missing from coerce_evidence_dict allowlist."
    )


@pytest.mark.asyncio
async def test_no_chat_id_skips_telegram(ev_doc):
    ctx = _ctx(chat_id=None)
    cluster = _cluster()
    triage = TriageResult(route="UNKNOWN_RESEARCH", cluster=cluster, urgency="critical")
    advisory = MagicMock(verdict="CRITICAL", trace_id="t", spec=["verdict", "trace_id"])
    with (
        patch("workers.remote_agent_pipeline.upsert_cluster", return_value=cluster),
        patch("workers.remote_agent_pipeline.triage_cluster", return_value=triage),
        patch("workers.remote_agent_pipeline.analyze_cluster", return_value=advisory),
        patch("workers.remote_agent_pipeline.write_lessons", new_callable=AsyncMock),
        patch("workers.remote_agent_pipeline.render_advisory_to_telegram", new_callable=AsyncMock) as mock_tg,
    ):
        await handle_remote_agent_evidence(ctx, ev_doc, "trace-008")
    mock_tg.assert_not_called()


@pytest.mark.asyncio
async def test_remote_baseline_writes_redis_key(ev_doc):
    """1C: remote_system_metrics evidence feeds the rolling 3σ baseline."""
    ctx = _ctx()
    ev_doc = {
        **ev_doc,
        "tenant_id": "t1",
        "namespace": "customer-host-1",
        "extracted_fact": {"agent_id": "agent-1", "cpu_percent": 95.0, "mem_percent": 40.0},
    }
    cluster = _cluster()
    triage = TriageResult(route="KNOWN_PATTERN", cluster=cluster, urgency="baseline")
    with (
        patch("workers.remote_agent_pipeline.upsert_cluster", return_value=cluster),
        patch("workers.remote_agent_pipeline.triage_cluster", return_value=triage),
        patch("workers.remote_agent_pipeline.write_lessons", new_callable=AsyncMock),
    ):
        await handle_remote_agent_evidence(ctx, ev_doc, "trace-1c")

    key = "3sigma:remote:t1:customer-host-1:cpu"
    samples = await ctx.redis.lrange(key, 0, -1)
    assert samples == ["95.0"]
    mem_key = "3sigma:remote:t1:customer-host-1:mem"
    assert await ctx.redis.lrange(mem_key, 0, -1) == ["40.0"]


@pytest.mark.asyncio
async def test_remote_baseline_enriches_zscore_after_window(ev_doc):
    """1C: after enough samples, z_cpu is attached to extracted_fact."""
    ctx = _ctx()
    cluster = _cluster()
    triage = TriageResult(route="KNOWN_PATTERN", cluster=cluster, urgency="baseline")
    base = {
        **ev_doc,
        "tenant_id": "t1",
        "namespace": "customer-host-2",
    }
    with (
        patch("workers.remote_agent_pipeline.upsert_cluster", return_value=cluster) as mock_up,
        patch("workers.remote_agent_pipeline.triage_cluster", return_value=triage),
        patch("workers.remote_agent_pipeline.write_lessons", new_callable=AsyncMock),
    ):
        for v in (10.0, 11.0, 9.0, 10.0):
            doc = {**base, "extracted_fact": {"agent_id": "agent-1", "cpu_percent": v}}
            await handle_remote_agent_evidence(ctx, doc, "trace-warm")
        spike = {**base, "extracted_fact": {"agent_id": "agent-1", "cpu_percent": 95.0}}
        await handle_remote_agent_evidence(ctx, spike, "trace-spike")

    # upsert_cluster(redis, agent_id, fp, ev_doc, domain) — ev_doc is arg index 3
    last_doc = mock_up.call_args.args[3]
    assert "z_cpu" in last_doc["extracted_fact"]
