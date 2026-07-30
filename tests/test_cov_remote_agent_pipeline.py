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
        # FAILED → an actual breach, so it always proceeds into the pipeline below.
        # (A PASSED/healthy remote_system_metrics sample is diverted to the
        # baseline_ok side-channel before stage 2 — see TestHealthyHeartbeatSkip.)
        "result": "FAILED",
        "extracted_fact": {"agent_id": "agent-1"},
    }


@pytest.mark.asyncio
async def test_cluster_upsert_fail_returns_empty(ev_doc):
    ctx = _ctx()
    with patch("workers.remote_agent_pipeline.upsert_cluster", side_effect=RuntimeError("redis down")):
        result = await handle_remote_agent_evidence(ctx, ev_doc, "trace-001")
    assert result == ""


@pytest.mark.asyncio
async def test_repeat_known_cluster_below_notify_tier_suppressed(ev_doc):
    """is_new=False + urgency below notify tier → no Active Trace, no advisory work.

    Without this guard a long-running condition (systemd unit down, disk over
    threshold, etc.) re-sends the same evidence every collection cycle and
    each one spawns a brand-new omni:trace:stages entry forever.
    """
    ctx = _ctx()
    cluster = _cluster(route="KNOWN_BASELINE", urgency="baseline")
    cluster.is_new = False
    triage = TriageResult(route="KNOWN_BASELINE", cluster=cluster, urgency="baseline")
    with (
        patch("workers.remote_agent_pipeline.upsert_cluster", return_value=cluster),
        patch("workers.remote_agent_pipeline.triage_cluster", return_value=triage),
        patch("workers.remote_agent_pipeline.write_lessons", new_callable=AsyncMock) as mock_lessons,
        patch("workers.remote_agent_pipeline.analyze_cluster") as mock_llm,
        patch("workers.remote_agent_pipeline.mark_stage", new_callable=AsyncMock) as mock_mark,
    ):
        result = await handle_remote_agent_evidence(ctx, ev_doc, "trace-repeat-1")
    assert result == "remote_agent:KNOWN_BASELINE:repeat_suppressed"
    mock_mark.assert_not_called()
    mock_llm.assert_not_called()
    mock_lessons.assert_not_called()
    assert await ctx.redis.keys("omni:trace:stages:*") == []


@pytest.mark.asyncio
async def test_repeat_known_cluster_above_notify_tier_still_proceeds(ev_doc):
    """is_new=False but urgency=critical/high → still goes through the pipeline
    (an escalating/ongoing critical issue must keep being tracked, not silenced)."""
    ctx = _ctx()
    cluster = _cluster(route="KNOWN_BASELINE", urgency="critical")
    cluster.is_new = False
    triage = TriageResult(route="KNOWN_BASELINE", cluster=cluster, urgency="critical")
    with (
        patch("workers.remote_agent_pipeline.upsert_cluster", return_value=cluster),
        patch("workers.remote_agent_pipeline.triage_cluster", return_value=triage),
        patch("workers.remote_agent_pipeline.write_lessons", new_callable=AsyncMock),
        # return_value=MagicMock(): advisory downstream gọi model_dump() sync —
        # để AsyncMock trần thì model_dump() sinh coroutine không được await
        patch("workers.remote_agent_pipeline.analyze_cluster", new=AsyncMock(return_value=MagicMock())) as mock_llm,
    ):
        result = await handle_remote_agent_evidence(ctx, ev_doc, "trace-repeat-2")
    mock_llm.assert_called_once()
    assert "repeat_suppressed" not in result


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
        "result": "FAILED",
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
        for v in (10.0, 11.0, 9.0, 10.0, 10.5, 9.5, 10.0, 11.0, 9.0):
            doc = {**base, "extracted_fact": {"agent_id": "agent-1", "cpu_percent": v}}
            await handle_remote_agent_evidence(ctx, doc, "trace-warm")
        spike = {**base, "extracted_fact": {"agent_id": "agent-1", "cpu_percent": 95.0}}
        await handle_remote_agent_evidence(ctx, spike, "trace-spike")

    # upsert_cluster(redis, agent_id, fp, ev_doc, domain) — ev_doc is arg index 3
    last_doc = mock_up.call_args.args[3]
    assert "z_cpu" in last_doc["extracted_fact"]


class TestHealthyHeartbeatSkip:
    """A healthy remote_system_metrics sample must never reach Active Traces —
    it is parked in the baseline_ok side-channel instead (no mark_stage, no
    cluster/triage call)."""

    @pytest.mark.asyncio
    async def test_passed_heartbeat_skips_pipeline_and_stores_side_channel(self):
        ctx = _ctx()
        ev = {
            "evidence_source": "RemoteAgent",
            "probe": "remote_system_metrics",
            "alert_hint": "CPU=12.0% MEM=30.0% DISK=40.0%",
            "raw": "",
            "lane": "SYS_RESOURCE",
            "alert_rule": "RemoteSystemNormal",
            "namespace": "customer-host-3",
            "tenant_id": "t1",
            "result": "PASSED",
            "extracted_fact": {"agent_id": "agent-9", "cpu_percent": 12.0, "mem_percent": 30.0},
        }
        with (
            patch("workers.remote_agent_pipeline.upsert_cluster") as mock_up,
            patch("workers.remote_agent_pipeline.triage_cluster") as mock_triage,
        ):
            result = await handle_remote_agent_evidence(ctx, ev, "trace-healthy-1")

        assert result == ""
        mock_up.assert_not_called()
        mock_triage.assert_not_called()

        raw = await ctx.redis.get("omni:remote_agent:baseline_ok:agent-9")
        assert raw is not None
        import json as _json
        snapshot = _json.loads(raw)
        assert snapshot["fact"]["cpu_percent"] == 12.0

        # No pipeline stage was ever recorded for this trace.
        stages = await ctx.redis.hgetall("omni:trace:stages:trace-healthy-1")
        assert stages == {}

    @pytest.mark.asyncio
    async def test_failed_heartbeat_still_proceeds_through_pipeline(self):
        """A real threshold breach (result=FAILED) must NOT be diverted."""
        ctx = _ctx()
        cluster = _cluster()
        triage = TriageResult(route="KNOWN_PATTERN", cluster=cluster, urgency="baseline")
        ev = {
            "evidence_source": "RemoteAgent",
            "probe": "remote_system_metrics",
            "alert_hint": "CPU 96%>80%",
            "raw": "",
            "lane": "SYS_RESOURCE",
            "alert_rule": "RemoteSystemAnomaly",
            "namespace": "customer-host-4",
            "tenant_id": "t1",
            "result": "FAILED",
            "extracted_fact": {"agent_id": "agent-10", "cpu_percent": 96.0},
        }
        with (
            patch("workers.remote_agent_pipeline.upsert_cluster", return_value=cluster) as mock_up,
            patch("workers.remote_agent_pipeline.triage_cluster", return_value=triage),
            patch("workers.remote_agent_pipeline.write_lessons", new_callable=AsyncMock),
        ):
            await handle_remote_agent_evidence(ctx, ev, "trace-failed-1")

        mock_up.assert_called_once()


class TestLogErrorsBaselineDivert:
    """remote_log_errors PASSED (no surge) must be diverted to side-channel,
    never entering the cluster/triage pipeline."""

    @pytest.mark.asyncio
    async def test_passed_log_errors_stores_side_channel_not_pipeline(self):
        from fakeredis.aioredis import FakeRedis
        import json as _json
        ctx = _ctx()
        ev = {
            "evidence_source": "RemoteAgent",
            "probe": "remote_log_errors",
            "alert_hint": "[host1] log errors: 3 files scanned, all clean",
            "raw": "",
            "lane": "APP_HTTP",
            "alert_rule": "RemoteLogNormal",
            "namespace": "host1",
            "result": "PASSED",
            "extracted_fact": {"agent_id": "agent-log-1", "files_scanned": 3, "failed_file_count": 0},
        }
        with (
            patch("workers.remote_agent_pipeline.upsert_cluster") as mock_up,
            patch("workers.remote_agent_pipeline.triage_cluster") as mock_triage,
        ):
            result = await handle_remote_agent_evidence(ctx, ev, "trace-log-pass-1")

        assert result == ""
        mock_up.assert_not_called()
        mock_triage.assert_not_called()

        raw = await ctx.redis.get("omni:remote_agent:log_baseline:agent-log-1")
        assert raw is not None
        snapshot = _json.loads(raw)
        assert snapshot["fact"]["files_scanned"] == 3
        assert snapshot["fact"]["failed_file_count"] == 0

    @pytest.mark.asyncio
    async def test_failed_log_errors_new_cluster_proceeds_through_pipeline(self):
        """A new FAILED log surge must go through the full pipeline."""
        ctx = _ctx()
        cluster = _cluster(domain=DOMAIN_CONTAINER)
        cluster.is_new = True
        triage = TriageResult(route="UNKNOWN_RESEARCH", cluster=cluster, urgency="medium")
        ev = {
            "evidence_source": "RemoteAgent",
            "probe": "remote_log_errors",
            "alert_hint": "[host1] log errors: 2/3 files over threshold",
            "raw": "",
            "lane": "APP_HTTP",
            "alert_rule": "RemoteLogErrorSurge",
            "namespace": "host1",
            "result": "FAILED",
            "extracted_fact": {"agent_id": "agent-log-2", "failed_file_count": 2},
        }
        with (
            patch("workers.remote_agent_pipeline.upsert_cluster", return_value=cluster) as mock_up,
            patch("workers.remote_agent_pipeline.triage_cluster", return_value=triage),
            patch("workers.remote_agent_pipeline.write_lessons", new_callable=AsyncMock),
            patch("workers.remote_agent_pipeline.analyze_cluster", new=AsyncMock(return_value=MagicMock())),
        ):
            await handle_remote_agent_evidence(ctx, ev, "trace-log-fail-new")

        mock_up.assert_called_once()

    @pytest.mark.asyncio
    async def test_failed_log_errors_repeat_medium_fast_path_suppressed(self):
        """Repeat FAILED log surge with medium urgency must exit BEFORE triage
        (no RAG round-trip), keeping the main pipeline clean."""
        ctx = _ctx()
        cluster = _cluster(domain=DOMAIN_CONTAINER)
        # Simulate a repeat cluster with only FAILED results → failed_ratio = 1.0
        cluster.is_new = False
        cluster.count = 3
        cluster.results = __import__("collections").Counter({"FAILED": 3})
        # Use a neutral alert_hint that won't match high/critical domain signals
        cluster.representative = {
            "alert_hint": "[host1] log errors: 1/3 files over threshold — ['/var/log/syslog']",
            "probe": "remote_log_errors",
        }
        ev = {
            "evidence_source": "RemoteAgent",
            "probe": "remote_log_errors",
            "alert_hint": "[host1] log errors: 1/3 files over threshold",
            "raw": "",
            "lane": "APP_HTTP",
            "alert_rule": "RemoteLogErrorSurge",
            "namespace": "host1",
            "result": "FAILED",
            "extracted_fact": {"agent_id": "agent-log-3", "failed_file_count": 1},
        }
        with (
            patch("workers.remote_agent_pipeline.upsert_cluster", return_value=cluster),
            patch("workers.remote_agent_pipeline.triage_cluster") as mock_triage,
            patch("workers.remote_agent_pipeline.mark_stage", new_callable=AsyncMock) as mock_mark,
        ):
            result = await handle_remote_agent_evidence(ctx, ev, "trace-log-fail-repeat")

        # Fast-path must have fired: triage (RAG) never called, no Active Trace
        mock_triage.assert_not_called()
        mock_mark.assert_not_called()
        assert "repeat_suppressed" in result
