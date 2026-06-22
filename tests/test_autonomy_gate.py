"""Tests for the Autonomy Engine: AutonomyPolicyStore + AutonomyGate."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from pkg.autonomy.policy import AutonomyLevel, AutonomyPolicyStore, PolicyRule
from pkg.autonomy.gate import AutonomyGate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _make_ctx(redis=None, kafka=None):
    if redis is None:
        redis = _make_redis()
    if kafka is None:
        kafka = MagicMock()
        kafka.send_dict = AsyncMock()
    return SimpleNamespace(redis=redis, kafka=kafka, settings=None)


# ---------------------------------------------------------------------------
# 1. AutonomyPolicyStore — basic get/set
# ---------------------------------------------------------------------------

class TestAutonomyPolicyStore:
    @pytest.mark.asyncio
    async def test_get_policy_returns_defaults_when_redis_empty(self):
        """Empty Redis → DEFAULT_POLICY is returned."""
        store = AutonomyPolicyStore()
        redis = _make_redis()
        rules = await store.get_policy(redis)
        assert len(rules) > 0
        # Last rule is the catch-all SUGGEST_ONLY
        last = rules[-1]
        assert last.lane == "*"
        assert last.level == AutonomyLevel.SUGGEST_ONLY

    @pytest.mark.asyncio
    async def test_policy_update_stored_in_redis(self):
        """After set_rule(), get_policy() returns the new rule as the first entry."""
        store = AutonomyPolicyStore()
        redis = _make_redis()

        custom_rule = PolicyRule(
            lane="APP_HTTP",
            severity="high",
            action_type="scale_replicas",
            level=AutonomyLevel.HITL,
            reason="test rule",
        )
        await store.set_rule(redis, custom_rule)

        rules = await store.get_policy(redis)
        assert rules[0].lane == "APP_HTTP"
        assert rules[0].severity == "high"
        assert rules[0].action_type == "scale_replicas"
        assert rules[0].level == AutonomyLevel.HITL

    @pytest.mark.asyncio
    async def test_policy_history_tracks_changes(self):
        """Two set_rule calls produce 2 history entries."""
        store = AutonomyPolicyStore()
        redis = _make_redis()

        rule_a = PolicyRule(lane="SYS_RESOURCE", severity="low", action_type="*", level=AutonomyLevel.FULL_AUTO)
        rule_b = PolicyRule(lane="SIEM_SECURITY", severity="high", action_type="*", level=AutonomyLevel.HITL)

        await store.set_rule(redis, rule_a)
        await store.set_rule(redis, rule_b)

        history = await store.get_history(redis, limit=50)
        assert len(history) >= 2
        # Most recent first
        assert history[0]["rule"]["lane"] == "SIEM_SECURITY"
        assert history[1]["rule"]["lane"] == "SYS_RESOURCE"

    @pytest.mark.asyncio
    async def test_reset_to_defaults_restores_defaults(self):
        """reset_to_defaults() removes custom rules and puts back the built-in policy."""
        store = AutonomyPolicyStore()
        redis = _make_redis()

        # Add a custom rule that would change order
        custom = PolicyRule(lane="APP_HTTP", severity="critical", action_type="*", level=AutonomyLevel.ALERT_ONLY)
        await store.set_rule(redis, custom)

        # Now reset
        await store.reset_to_defaults(redis)
        rules = await store.get_policy(redis)

        # Should match DEFAULT_POLICY exactly
        default_rules = AutonomyPolicyStore.DEFAULT_POLICY
        assert len(rules) == len(default_rules)
        for actual, expected in zip(rules, default_rules):
            assert actual.lane == expected.lane
            assert actual.severity == expected.severity
            assert actual.level == expected.level


# ---------------------------------------------------------------------------
# 2. AutonomyGate — policy resolution
# ---------------------------------------------------------------------------

class TestAutonomyGate:
    @pytest.mark.asyncio
    async def test_default_policy_suggest_only(self):
        """No custom policy: generic (SYS_RESOURCE, high, delete_pod) → SUGGEST_ONLY."""
        gate = AutonomyGate()
        ctx = _make_ctx()

        level = await gate.evaluate(
            lane="SYS_RESOURCE",
            severity="high",
            action_type="delete_pod",
            fp_rate=0.0,
            ctx=ctx,
            trace_id="test-001",
        )
        assert level == AutonomyLevel.SUGGEST_ONLY

    @pytest.mark.asyncio
    async def test_full_auto_for_safe_restart(self):
        """Default policy: (any_lane, any_severity, restart_pod) → FULL_AUTO."""
        gate = AutonomyGate()
        ctx = _make_ctx()

        level = await gate.evaluate(
            lane="SYS_HARD_FAIL",
            severity="critical",
            action_type="restart_pod",
            fp_rate=0.0,
            ctx=ctx,
            trace_id="test-002",
        )
        assert level == AutonomyLevel.FULL_AUTO

    @pytest.mark.asyncio
    async def test_hitl_for_siem_critical(self):
        """Default policy: (SIEM_SECURITY, critical, block_ip) → HITL."""
        gate = AutonomyGate()
        ctx = _make_ctx()

        level = await gate.evaluate(
            lane="SIEM_SECURITY",
            severity="critical",
            action_type="block_ip",
            fp_rate=0.0,
            ctx=ctx,
            trace_id="test-003",
        )
        assert level == AutonomyLevel.HITL

    @pytest.mark.asyncio
    async def test_fp_rate_escalates_full_auto_to_suggest(self):
        """fp_rate=0.20 > threshold(0.15): FULL_AUTO → SUGGEST_ONLY."""
        gate = AutonomyGate()
        ctx = _make_ctx()

        # restart_pod is normally FULL_AUTO
        level = await gate.evaluate(
            lane="SYS_RESOURCE",
            severity="medium",
            action_type="restart_pod",
            fp_rate=0.20,
            ctx=ctx,
            trace_id="test-004",
        )
        assert level == AutonomyLevel.SUGGEST_ONLY

    @pytest.mark.asyncio
    async def test_fp_rate_does_not_affect_hitl(self):
        """fp_rate=0.20 with HITL policy → still HITL (security cannot be downgraded)."""
        gate = AutonomyGate()
        ctx = _make_ctx()

        level = await gate.evaluate(
            lane="SIEM_SECURITY",
            severity="critical",
            action_type="block_ip",
            fp_rate=0.20,
            ctx=ctx,
            trace_id="test-005",
        )
        assert level == AutonomyLevel.HITL

    @pytest.mark.asyncio
    async def test_fp_rate_at_threshold_does_not_escalate(self):
        """fp_rate exactly at threshold (0.15) does NOT escalate (strictly greater than)."""
        gate = AutonomyGate()
        ctx = _make_ctx()

        level = await gate.evaluate(
            lane="SYS_RESOURCE",
            severity="low",
            action_type="restart_pod",
            fp_rate=0.15,
            ctx=ctx,
            trace_id="test-006",
        )
        assert level == AutonomyLevel.FULL_AUTO

    @pytest.mark.asyncio
    async def test_autonomy_decision_written_to_crat(self):
        """After gate.evaluate(), CRAT block with event_type=AUTONOMY_DECISION exists in Redis."""
        gate = AutonomyGate()
        redis = _make_redis()
        ctx = _make_ctx(redis=redis)

        await gate.evaluate(
            lane="APP_HTTP",
            severity="high",
            action_type="scale_replicas",
            fp_rate=0.0,
            ctx=ctx,
            trace_id="test-007",
        )

        # Verify CRAT block was written
        blocks_raw = await redis.lrange("audit_chain:blocks", 0, -1)
        assert len(blocks_raw) >= 1

        last_block = json.loads(blocks_raw[-1])
        assert last_block["event_type"] == "AUTONOMY_DECISION"
        assert last_block["trace_id"] == "test-007"
        assert last_block["payload"]["lane"] == "APP_HTTP"
        assert last_block["payload"]["severity"] == "high"
        assert last_block["payload"]["action_type"] == "scale_replicas"

    @pytest.mark.asyncio
    async def test_crat_block_contains_resolved_level(self):
        """CRAT block payload includes the resolved autonomy level."""
        gate = AutonomyGate()
        redis = _make_redis()
        ctx = _make_ctx(redis=redis)

        await gate.evaluate(
            lane="SIEM_SECURITY",
            severity="critical",
            action_type="block_ip",
            fp_rate=0.0,
            ctx=ctx,
            trace_id="test-008",
        )

        blocks_raw = await redis.lrange("audit_chain:blocks", 0, -1)
        last_block = json.loads(blocks_raw[-1])
        assert last_block["payload"]["resolved_level"] == "HITL"

    @pytest.mark.asyncio
    async def test_crat_records_fp_escalation(self):
        """CRAT block records fp_escalated=True when FP rate triggers escalation."""
        gate = AutonomyGate()
        redis = _make_redis()
        ctx = _make_ctx(redis=redis)

        await gate.evaluate(
            lane="SYS_RESOURCE",
            severity="medium",
            action_type="restart_pod",
            fp_rate=0.25,
            ctx=ctx,
            trace_id="test-009",
        )

        blocks_raw = await redis.lrange("audit_chain:blocks", 0, -1)
        last_block = json.loads(blocks_raw[-1])
        assert last_block["payload"]["fp_escalated"] is True
        assert last_block["payload"]["original_level"] == "FULL_AUTO"
        assert last_block["payload"]["resolved_level"] == "SUGGEST_ONLY"

    @pytest.mark.asyncio
    async def test_custom_rule_takes_priority_over_default(self):
        """Custom rule prepended to policy takes priority over default catch-all."""
        gate = AutonomyGate()
        redis = _make_redis()
        ctx = _make_ctx(redis=redis)

        # Set a custom rule: APP_HTTP + critical + * → HITL
        store = AutonomyPolicyStore()
        custom = PolicyRule(
            lane="APP_HTTP",
            severity="critical",
            action_type="*",
            level=AutonomyLevel.HITL,
            reason="critical http needs HITL",
        )
        await store.set_rule(redis, custom)

        level = await gate.evaluate(
            lane="APP_HTTP",
            severity="critical",
            action_type="scale_replicas",
            fp_rate=0.0,
            ctx=ctx,
            trace_id="test-010",
        )
        assert level == AutonomyLevel.HITL

    @pytest.mark.asyncio
    async def test_scale_replicas_full_auto_by_default(self):
        """Default policy: scale_replicas is FULL_AUTO for any lane/severity."""
        gate = AutonomyGate()
        ctx = _make_ctx()

        level = await gate.evaluate(
            lane="APP_HTTP",
            severity="critical",
            action_type="scale_replicas",
            fp_rate=0.0,
            ctx=ctx,
            trace_id="test-011",
        )
        assert level == AutonomyLevel.FULL_AUTO


# ---------------------------------------------------------------------------
# 3. AutonomyGate.get_fp_rate_for_lane
# ---------------------------------------------------------------------------

class TestGetFpRate:
    @pytest.mark.asyncio
    async def test_returns_zero_when_no_kpi_data(self):
        """Empty Redis KPI keys → fp_rate = 0.0."""
        gate = AutonomyGate()
        redis = _make_redis()
        rate = await gate.get_fp_rate_for_lane("SYS_RESOURCE", redis)
        assert rate == 0.0

    @pytest.mark.asyncio
    async def test_computes_rate_from_kpi_keys(self):
        """fp_rate = false_positive / (accepted + false_positive)."""
        import time

        gate = AutonomyGate()
        redis = _make_redis()
        now = time.time()

        # Add 4 accepted, 1 false_positive → rate = 1/5 = 0.2
        for i in range(4):
            await redis.zadd("omni:kpi:z:accepted", {f"a{i}": now})
        await redis.zadd("omni:kpi:z:false_positive", {"fp0": now})

        rate = await gate.get_fp_rate_for_lane("SYS_RESOURCE", redis)
        assert abs(rate - 0.2) < 1e-9

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_false_positives(self):
        """Pure accepted records → fp_rate = 0.0."""
        import time

        gate = AutonomyGate()
        redis = _make_redis()
        now = time.time()

        for i in range(10):
            await redis.zadd("omni:kpi:z:accepted", {f"a{i}": now})

        rate = await gate.get_fp_rate_for_lane("APP_HTTP", redis)
        assert rate == 0.0

    @pytest.mark.asyncio
    async def test_returns_zero_when_redis_is_none(self):
        """None redis → safe fallback 0.0."""
        gate = AutonomyGate()
        rate = await gate.get_fp_rate_for_lane("SIEM_SECURITY", None)
        assert rate == 0.0
