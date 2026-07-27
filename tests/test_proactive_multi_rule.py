"""TDD: proactive_observer hỗ trợ NHIỀU PromQL rule (Phase 2, bug omni-core —
trước đây chỉ theo dõi 1 rule CrashLoopBackOff hardcode, gần như không bao giờ trigger
trong lab). ``proactive_promql_rules`` (JSON) rỗng/lỗi -> fallback fail-closed về đúng
1 rule cũ (không breaking config hiện có)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest

import workers.proactive_observer as po


def _settings(**overrides):
    base = dict(
        proactive_promql="sum(kube_pod_container_status_waiting_reason{reason=\"CrashLoopBackOff\"})",
        proactive_trigger_threshold=0.0,
        proactive_promql_rules="",
        proactive_cooldown_sec=60,
        proactive_kill_switch_key="omni:proactive:kill_switch",
        kafka_topic_proactive_incidents="omni-proactive-incidents",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestLoadProactiveRules:
    def test_empty_config_falls_back_to_single_legacy_rule(self):
        ws = _settings()
        rules = po._load_proactive_rules(ws)
        assert len(rules) == 1
        assert rules[0].name == po.DEFAULT_RULE
        assert rules[0].promql == ws.proactive_promql
        assert rules[0].threshold == ws.proactive_trigger_threshold

    def test_valid_json_parses_multiple_rules(self):
        import json

        rules_json = json.dumps([
            {"name": "oom_risk", "promql": "container_oom_events_total", "threshold": 0.0},
            {"name": "disk_pressure", "promql": "node_disk_pct", "threshold": 90.0},
        ])
        ws = _settings(proactive_promql_rules=rules_json)
        rules = po._load_proactive_rules(ws)
        assert [r.name for r in rules] == ["oom_risk", "disk_pressure"]
        assert rules[1].threshold == 90.0

    def test_malformed_json_falls_back_fail_closed(self):
        ws = _settings(proactive_promql_rules="{not valid json")
        rules = po._load_proactive_rules(ws)
        assert len(rules) == 1
        assert rules[0].name == po.DEFAULT_RULE

    def test_entries_without_promql_are_skipped(self):
        import json

        ws = _settings(proactive_promql_rules=json.dumps([{"name": "no_query"}]))
        rules = po._load_proactive_rules(ws)
        assert len(rules) == 1
        assert rules[0].name == po.DEFAULT_RULE  # skipped invalid entry -> empty -> fallback


@pytest.mark.asyncio
class TestEvaluateProactiveTriggersMultiRule:
    async def test_multiple_rules_each_independently_fire(self):
        import json

        rules_json = json.dumps([
            {"name": "rule_a", "promql": "metric_a", "threshold": 0.0},
            {"name": "rule_b", "promql": "metric_b", "threshold": 0.0},
        ])
        ws = _settings(proactive_promql_rules=rules_json)
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        kafka = AsyncMock()
        ctx = SimpleNamespace(settings=ws, redis=r, kafka=kafka)

        async def fake_instant(ctx_, promql):
            return 5.0  # above threshold=0.0 for both rules

        with patch.object(po, "_instant_scalar", side_effect=fake_instant):
            fired = await po.evaluate_proactive_triggers(ctx)

        assert fired == 2
        assert kafka.send_envelope_inner.await_count == 2

    async def test_one_rule_on_cooldown_other_still_fires(self):
        import json

        rules_json = json.dumps([
            {"name": "rule_a", "promql": "metric_a", "threshold": 0.0},
            {"name": "rule_b", "promql": "metric_b", "threshold": 0.0},
        ])
        ws = _settings(proactive_promql_rules=rules_json)
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        kafka = AsyncMock()
        ctx = SimpleNamespace(settings=ws, redis=r, kafka=kafka)

        dedupe = f"rule_a:{'metric_a'[:120]}"
        ck = f"omni:proactive:cooldown:{hash(dedupe) & 0xFFFFFFFF:X}"
        await r.setex(ck, 60, "1")

        async def fake_instant(ctx_, promql):
            return 5.0

        with patch.object(po, "_instant_scalar", side_effect=fake_instant):
            fired = await po.evaluate_proactive_triggers(ctx)

        assert fired == 1
        assert kafka.send_envelope_inner.await_count == 1

    async def test_kill_switch_short_circuits_all_rules(self):
        import json

        rules_json = json.dumps([{"name": "rule_a", "promql": "metric_a", "threshold": 0.0}])
        ws = _settings(proactive_promql_rules=rules_json)
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await r.set(ws.proactive_kill_switch_key, "1")
        kafka = AsyncMock()
        ctx = SimpleNamespace(settings=ws, redis=r, kafka=kafka)

        fired = await po.evaluate_proactive_triggers(ctx)
        assert fired == 0
        kafka.send_envelope_inner.assert_not_awaited()
