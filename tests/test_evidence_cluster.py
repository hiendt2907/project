"""Tests for evidence_cluster.py — LogCluster building and Redis state."""

from __future__ import annotations

import time

import pytest
from fakeredis.aioredis import FakeRedis

from pkg.reasoning.evidence_cluster import (
    STORM_THRESHOLD,
    LogCluster,
    get_seen_state,
    mark_cluster_diagnosed,
    upsert_cluster,
)
from pkg.reasoning.domain_signals import DOMAIN_OS, DOMAIN_DATABASE


def _item(probe: str = "remote_log_errors", result: str = "FAILED",
          alert_hint: str = "OOM kill: mysqld", lane: str = "SYS_RESOURCE") -> dict:
    return {
        "probe": probe,
        "result": result,
        "alert_hint": alert_hint,
        "raw": "",
        "lane": lane,
        "extracted_fact": {},
        "trace_id": "test-trace-001",
    }


class TestUpsertCluster:
    @pytest.mark.asyncio
    async def test_first_occurrence_is_new(self):
        redis = FakeRedis(decode_responses=True)
        cluster = await upsert_cluster(
            redis, "agent-1", "fp:abc123def456",
            _item(), DOMAIN_OS,
        )
        assert cluster.is_new is True
        assert cluster.count == 1
        assert cluster.is_storm is False

    @pytest.mark.asyncio
    async def test_second_occurrence_not_new(self):
        redis = FakeRedis(decode_responses=True)
        fp = "fp:abc123def456"
        await upsert_cluster(redis, "agent-1", fp, _item(), DOMAIN_OS)
        cluster = await upsert_cluster(redis, "agent-1", fp, _item(), DOMAIN_OS)
        assert cluster.is_new is False
        assert cluster.count == 2

    @pytest.mark.asyncio
    async def test_count_increments_per_agent_window(self):
        redis = FakeRedis(decode_responses=True)
        fp = "fp:deadbeef1234"
        for _ in range(5):
            cluster = await upsert_cluster(redis, "agent-1", fp, _item(), DOMAIN_OS)
        assert cluster.count == 5

    @pytest.mark.asyncio
    async def test_storm_detection(self):
        redis = FakeRedis(decode_responses=True)
        fp = "fp:stormtest1234"
        for i in range(STORM_THRESHOLD + 1):
            cluster = await upsert_cluster(redis, "agent-1", fp, _item(), DOMAIN_OS)
        assert cluster.is_storm is True

    @pytest.mark.asyncio
    async def test_below_storm_threshold_not_storm(self):
        redis = FakeRedis(decode_responses=True)
        fp = "fp:belowstorm1234"
        for _ in range(STORM_THRESHOLD):
            cluster = await upsert_cluster(redis, "agent-1", fp, _item(), DOMAIN_OS)
        assert cluster.is_storm is False

    @pytest.mark.asyncio
    async def test_result_distribution_tracked(self):
        redis = FakeRedis(decode_responses=True)
        fp = "fp:results1234"
        await upsert_cluster(redis, "a1", fp, _item(result="FAILED"), DOMAIN_OS)
        await upsert_cluster(redis, "a1", fp, _item(result="FAILED"), DOMAIN_OS)
        cluster = await upsert_cluster(redis, "a1", fp, _item(result="PASSED"), DOMAIN_OS)
        assert cluster.results["FAILED"] == 2
        assert cluster.results["PASSED"] == 1

    @pytest.mark.asyncio
    async def test_representative_updated_to_richest(self):
        redis = FakeRedis(decode_responses=True)
        fp = "fp:repr1234"
        await upsert_cluster(redis, "a1", fp,
                              _item(alert_hint="short"), DOMAIN_OS)
        cluster = await upsert_cluster(redis, "a1", fp,
                                       _item(alert_hint="much longer alert hint with real context"), DOMAIN_OS)
        assert cluster.representative["alert_hint"] == "much longer alert hint with real context"

    @pytest.mark.asyncio
    async def test_multiple_agents_tracked_in_seen_state(self):
        redis = FakeRedis(decode_responses=True)
        fp = "fp:multiagent1234"
        # Each agent maintains its own per-agent window; cross-agent tracking in seen_state
        await upsert_cluster(redis, "agent-1", fp, _item(), DOMAIN_OS)
        await upsert_cluster(redis, "agent-2", fp, _item(), DOMAIN_OS)
        seen = await get_seen_state(redis, fp)
        assert seen is not None
        assert "agent-1" in seen["agents"]
        assert "agent-2" in seen["agents"]
        assert seen["total_count"] == 2

    @pytest.mark.asyncio
    async def test_cross_agent_seen_state_is_new_first_time(self):
        redis = FakeRedis(decode_responses=True)
        fp = "fp:seentest1234"
        cluster = await upsert_cluster(redis, "agent-1", fp, _item(), DOMAIN_OS)
        assert cluster.is_new is True

    @pytest.mark.asyncio
    async def test_cross_agent_not_new_on_second_agent(self):
        redis = FakeRedis(decode_responses=True)
        fp = "fp:crossagent1234"
        await upsert_cluster(redis, "agent-1", fp, _item(), DOMAIN_OS)
        # Second agent — cross-agent key already exists → not new
        cluster = await upsert_cluster(redis, "agent-2", fp, _item(), DOMAIN_OS)
        assert cluster.is_new is False

    @pytest.mark.asyncio
    async def test_domain_and_lane_preserved(self):
        redis = FakeRedis(decode_responses=True)
        cluster = await upsert_cluster(
            redis, "agent-1", "fp:domain1234",
            _item(probe="mysql_status", lane="SYS_HARD_FAIL"), DOMAIN_DATABASE,
        )
        assert cluster.domain == DOMAIN_DATABASE
        assert cluster.lane == "SYS_HARD_FAIL"
        assert cluster.probe == "mysql_status"


class TestMarkClusterDiagnosed:
    @pytest.mark.asyncio
    async def test_updates_last_diagnosis(self):
        redis = FakeRedis(decode_responses=True)
        fp = "fp:diag1234"
        await upsert_cluster(redis, "agent-1", fp, _item(), DOMAIN_OS)
        await mark_cluster_diagnosed(redis, fp, "INVESTIGATE", "OOM kill from mysqld")

        state = await get_seen_state(redis, fp)
        assert state is not None
        assert state["last_diagnosis"]["verdict"] == "INVESTIGATE"
        assert "OOM kill" in state["last_diagnosis"]["root_cause"]

    @pytest.mark.asyncio
    async def test_no_error_when_fingerprint_not_seen(self):
        redis = FakeRedis(decode_responses=True)
        # Should not raise even if key doesn't exist
        await mark_cluster_diagnosed(redis, "fp:nonexistent", "NORMAL", "fine")


class TestGetSeenState:
    @pytest.mark.asyncio
    async def test_returns_none_when_not_seen(self):
        redis = FakeRedis(decode_responses=True)
        state = await get_seen_state(redis, "fp:neverseen")
        assert state is None

    @pytest.mark.asyncio
    async def test_returns_state_after_upsert(self):
        redis = FakeRedis(decode_responses=True)
        fp = "fp:seencheck1234"
        await upsert_cluster(redis, "agent-1", fp, _item(), DOMAIN_OS)
        state = await get_seen_state(redis, fp)
        assert state is not None
        assert state["total_count"] == 1
        assert "agent-1" in state["agents"]
