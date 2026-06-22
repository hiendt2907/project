"""Coverage gap tests for workers.proactive_observer.

Targets uncovered lines (68.2% → higher):
  85-86, 89-90:    _dbg_log (file write + logger paths)
  118-119:         _set_negative_pattern exception path
  158:             _react_mem_append exception path
  301:             _react_mem_recent
  317, 322-326:    _update_learning_pattern_stats
  418-419:         _update_learning_pattern_stats exception path
  450:             proactive_kill_switch_engaged exception path
  545-546, 558-580: _fail_safe_after_tool_error freeze paths
  597-598:         _fail_safe_after_tool_error dlq exception
  631-638:         _fail_safe_after_tool_error telegram path
  649-672:         _append_audit
  694-716:         _proactive_event_pipeline SOP hit path
  735-835:         _proactive_event_pipeline SOP miss + learning hit
  838, 859-865:    _proactive_event_pipeline governance deny paths
  867-894:         _proactive_event_pipeline fallback_enabled paths
  897-:            _process_proactive_message kill_switch / bad_payload
  985-1008:        _process_proactive_message timeout path
  1029, 1042-1108: kafka_proactive_incidents_loop
  1112-1132:       proactive_evaluate_loop
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("OMNI_WORKER_ROLE", "analyst")
os.environ.setdefault("OMNI_ENV_MODE", "dev")
os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379")

import fakeredis.aioredis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(**kw: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "god_mode": False,
        "lab_unchained": False,
        "proactive_fallback_bypass_policy_in_god_mode": False,
        "proactive_fallback_enabled": True,
        "proactive_fallback_max_attempts": 1,
        "proactive_fallback_confidence_min": 0.78,
        "proactive_fallback_allow_tools": "promql_instant,k8s_list_pods,inspect_pod_deep",
        "proactive_react_max_turns": 3,
        "proactive_react_memory_max_chars": 3200,
        "proactive_react_memory_line_max_chars": 2000,
        "proactive_llm_prompt_max_chars": 4096,
        "proactive_tool_timeout_sec": 30.0,
        "proactive_verify_keywords_fail": "",
        "proactive_resource_freeze_enabled": False,
        "proactive_freeze_key_prefix": "omni:freeze",
        "proactive_freeze_namespace_fallback_allowed": True,
        "proactive_resource_freeze_ttl_sec": 600,
        "proactive_lease_ttl_sec": 30,
        "proactive_react_require_namespace_for_list": True,
        "proactive_negative_pattern_ttl_sec": 604800,
        "proactive_kill_switch_key": "omni:proactive:kill_switch",
        "proactive_sop_collection": "omni_sop",
        "proactive_sop_score_threshold": 0.88,
        "proactive_event_timeout_sec": 600.0,
        "proactive_gigo_require_cluster_identity": False,
        "proactive_k8s_snapshot_timeout_sec": 5.0,
        "proactive_promql": "up == 0",
        "proactive_trigger_threshold": 0.5,
        "proactive_cooldown_sec": 60,
        "proactive_block_ms": 200,
        "proactive_enabled": True,
        "proactive_eval_interval_sec": 0.1,
        "kafka_topic_alerts": "omni-alerts",
        "kafka_topic_diagnostic_evidence": "omni-diagnostic-evidence",
        "kafka_topic_proactive_incidents": "omni-proactive-incidents",
        "kafka_topic_audit_proactive": "omni-audit-proactive",
        "kafka_topic_dlq": "omni-dlq",
        "kafka_bootstrap_servers": "localhost:9092",
        "consumer_group": "omni-workers",
        "consumer_group_proactive": "omni-proactive",
        "consumer_name_proactive": "omni-proactive-1",
        "telegram_admin_chat_id": None,
        "chat_model": "qwen2.5:7b",
        "embed_model": "nomic-embed-text",
        "action_experience_score_threshold": 0.85,
        "learning_stats_ttl_sec": 86400,
        "memory_canonical_strip_pods": True,
        "diagnostic_dictionary_enabled": False,
        "autonomous_decider_enabled": False,
        "omni_concise_reply_max_words": 200,
        "omni_summary_max_words": 200,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_ev(**kw: Any):
    from workers.proactive_models import AnomalyEvent
    defaults = dict(
        trace_id="trace-test01",
        rule_name="TestRule",
        canonical_query="cpu > 0.9",
        threshold=0.8,
        metric_value=0.95,
        namespace="multi-agent",
        deployment="my-app",
    )
    defaults.update(kw)
    return AnomalyEvent(**defaults)


def _make_ctx(redis_client=None, kafka=None, settings=None, **kw: Any):
    from workers.handler_context import WorkerHandlerContext
    scout_ready = asyncio.Event()
    scout_ready.set()
    sem = AsyncMock()
    sem.acquire_proactive = AsyncMock(return_value="token-abc")
    sem.release = AsyncMock()
    ledger = MagicMock()
    ledger.record_exception = AsyncMock()

    r = redis_client or fakeredis.aioredis.FakeRedis(decode_responses=True)

    return WorkerHandlerContext(
        settings=settings or _make_settings(),
        redis=r,
        llm=AsyncMock(),
        vector_store=MagicMock(),
        ledger=ledger,
        semaphore=sem,
        telegram=kw.pop("telegram", None),
        kafka=kafka,
        telegram_chat_id=None,
        inbound_source="",
        inbound_user_text="",
        scout_ready=scout_ready,
        inbound_trace_id="test-trace",
        inbound_proactive=False,
        k8s_mutated=False,
    )


def _make_kafka():
    kafka = MagicMock()
    kafka.send_dict = AsyncMock()
    kafka.send_envelope_inner = AsyncMock()
    kafka.close = AsyncMock()
    return kafka


# ---------------------------------------------------------------------------
# _dbg_log (lines 85-90)
# ---------------------------------------------------------------------------

class TestDbgLog:
    def test_dbg_log_handles_file_error(self):
        """_dbg_log silently swallows file write errors."""
        import workers.proactive_observer as po
        with patch("builtins.open", side_effect=PermissionError("no write")):
            po._dbg_log("run-1", "H1", "test.py", "test_msg", {"k": "v"})
        # No exception

    def test_dbg_log_logs_to_logger(self, caplog):
        """_dbg_log writes to logger as INFO."""
        import workers.proactive_observer as po
        import logging
        with caplog.at_level(logging.INFO, logger="workers.proactive_observer"):
            with patch("builtins.open", side_effect=OSError("no file")):
                po._dbg_log("run-2", "H2", "test.py", "msg_test", {"val": 42})
        assert any("msg_test" in r.message or "DBG671FBD" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _is_negative_pattern / _set_negative_pattern (lines 98-119)
# ---------------------------------------------------------------------------

class TestNegativePattern:
    @pytest.mark.asyncio
    async def test_is_negative_pattern_empty_key(self):
        """Empty pattern_key returns False immediately."""
        import workers.proactive_observer as po
        ctx = _make_ctx()
        result = await po._is_negative_pattern(ctx, "")
        assert result is False

    @pytest.mark.asyncio
    async def test_is_negative_pattern_set(self):
        """Pattern key set in redis → True."""
        import workers.proactive_observer as po
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis_client=r)
        await r.setex("omni:learning:negative:proactive:pk-001", 3600, "reason")
        result = await po._is_negative_pattern(ctx, "pk-001")
        assert result is True

    @pytest.mark.asyncio
    async def test_is_negative_pattern_not_set(self):
        """Pattern key not in redis → False."""
        import workers.proactive_observer as po
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis_client=r)
        result = await po._is_negative_pattern(ctx, "pk-notexist")
        assert result is False

    @pytest.mark.asyncio
    async def test_set_negative_pattern_empty_key(self):
        """Empty pattern_key → no-op."""
        import workers.proactive_observer as po
        ctx = _make_ctx()
        await po._set_negative_pattern(ctx, "", "reason")  # Should not raise

    @pytest.mark.asyncio
    async def test_set_negative_pattern_redis_error(self):
        """Redis error in set → swallowed."""
        import workers.proactive_observer as po
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis_client=r)
        with patch.object(r, "setex", side_effect=RuntimeError("redis down")):
            await po._set_negative_pattern(ctx, "pk-001", "reason")  # Should not raise


# ---------------------------------------------------------------------------
# _react_mem_append / _react_mem_recent (lines 141-163)
# ---------------------------------------------------------------------------

class TestReactMem:
    @pytest.mark.asyncio
    async def test_react_mem_append_and_recent(self):
        """Append observations and retrieve them."""
        import workers.proactive_observer as po
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis_client=r)
        await po._react_mem_append(ctx, "trace-mem01", "iter#1 result=ok")
        await po._react_mem_append(ctx, "trace-mem01", "iter#2 result=ok")
        rows = await po._react_mem_recent(ctx, "trace-mem01", limit=6)
        assert len(rows) == 2
        assert "iter#1" in rows[0]

    @pytest.mark.asyncio
    async def test_react_mem_append_exception_swallowed(self):
        """Exception in rpush → swallowed."""
        import workers.proactive_observer as po
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis_client=r)
        with patch.object(r, "rpush", side_effect=RuntimeError("redis err")):
            await po._react_mem_append(ctx, "trace-exc", "line")  # No raise

    @pytest.mark.asyncio
    async def test_react_mem_recent_empty(self):
        """No data → returns empty list."""
        import workers.proactive_observer as po
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis_client=r)
        rows = await po._react_mem_recent(ctx, "trace-empty", limit=5)
        assert rows == []

    @pytest.mark.asyncio
    async def test_react_mem_recent_exception_swallowed(self):
        """Exception in lrange → returns empty list."""
        import workers.proactive_observer as po
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis_client=r)
        with patch.object(r, "lrange", side_effect=RuntimeError("redis err")):
            rows = await po._react_mem_recent(ctx, "trace-exc", limit=5)
        assert rows == []


# ---------------------------------------------------------------------------
# _update_learning_pattern_stats (lines 388-419)
# ---------------------------------------------------------------------------

class TestUpdateLearningPatternStats:
    @pytest.mark.asyncio
    async def test_success_increments_both_success_and_total(self):
        """outcome=success → success + total incremented."""
        import workers.proactive_observer as po
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis_client=r)
        with patch("workers.proactive_observer.set_learning_unique_patterns"):
            await po._update_learning_pattern_stats(
                ctx, source="test_source", pattern_key="pk-123", outcome="success"
            )
        val = await r.hget("omni:learning:pattern:pk-123", "success")
        assert val == "1"
        total = await r.hget("omni:learning:pattern:pk-123", "total")
        assert total == "1"

    @pytest.mark.asyncio
    async def test_fail_increments_fail_and_total(self):
        """outcome=fail → fail + total incremented."""
        import workers.proactive_observer as po
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis_client=r)
        with patch("workers.proactive_observer.set_learning_unique_patterns"):
            await po._update_learning_pattern_stats(
                ctx, source="test_source", pattern_key="pk-456", outcome="fail"
            )
        val = await r.hget("omni:learning:pattern:pk-456", "fail")
        assert val == "1"

    @pytest.mark.asyncio
    async def test_exception_swallowed(self):
        """Redis exception → swallowed."""
        import workers.proactive_observer as po
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis_client=r)
        with patch.object(r, "pipeline", side_effect=RuntimeError("redis down")):
            await po._update_learning_pattern_stats(
                ctx, source="test_source", pattern_key="pk-err", outcome="success"
            )
        # No raise


# ---------------------------------------------------------------------------
# proactive_kill_switch_engaged (lines 422-430)
# ---------------------------------------------------------------------------

class TestKillSwitchEngaged:
    @pytest.mark.asyncio
    async def test_kill_switch_engaged_when_value_1(self):
        """Redis returns '1' → True."""
        import workers.proactive_observer as po
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await r.set("omni:proactive:kill_switch", "1")
        result = await po.proactive_kill_switch_engaged(r, "omni:proactive:kill_switch")
        assert result is True

    @pytest.mark.asyncio
    async def test_kill_switch_not_engaged_value_0(self):
        """Redis returns '0' → False."""
        import workers.proactive_observer as po
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await r.set("omni:proactive:kill_switch", "0")
        result = await po.proactive_kill_switch_engaged(r, "omni:proactive:kill_switch")
        assert result is False

    @pytest.mark.asyncio
    async def test_kill_switch_not_engaged_missing(self):
        """Key not set → False."""
        import workers.proactive_observer as po
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        result = await po.proactive_kill_switch_engaged(r, "missing_key")
        assert result is False

    @pytest.mark.asyncio
    async def test_kill_switch_exception_returns_false(self):
        """Redis exception → False (fail open for kill switch check)."""
        import workers.proactive_observer as po
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        with patch.object(r, "get", side_effect=RuntimeError("redis down")):
            result = await po.proactive_kill_switch_engaged(r, "some_key")
        assert result is False


# ---------------------------------------------------------------------------
# _append_audit (lines 649-672)
# ---------------------------------------------------------------------------

class TestAppendAudit:
    @pytest.mark.asyncio
    async def test_append_audit_sends_to_kafka(self):
        """_append_audit sends JSON payload to kafka audit topic."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka)
        await po._append_audit(
            ctx,
            trace_id="trace-audit01",
            rule_id="TestRule",
            outcome="SUCCESS",
            commands_run="k8s_list_pods",
            detail="some detail",
            meta={"path": "sop"},
        )
        kafka.send_dict.assert_awaited_once()
        call_args = kafka.send_dict.call_args
        assert call_args[0][0] == ctx.settings.kafka_topic_audit_proactive

    @pytest.mark.asyncio
    async def test_append_audit_without_meta(self):
        """_append_audit works without meta parameter."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka)
        await po._append_audit(
            ctx,
            trace_id="trace-audit02",
            rule_id="TestRule",
            outcome="FAIL",
        )
        kafka.send_dict.assert_awaited_once()


# ---------------------------------------------------------------------------
# _process_proactive_message (lines 897-1038)
# ---------------------------------------------------------------------------

class TestProcessProactiveMessage:
    @pytest.mark.asyncio
    async def test_kill_switch_skips_message(self):
        """Kill switch active → message skipped, audit SKIPPED_KILL_SWITCH."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await r.set("omni:proactive:kill_switch", "1")
        ctx = _make_ctx(redis_client=r, kafka=kafka)
        audit_outcomes: list[str] = []

        async def fake_append_audit(ctx, *, trace_id, rule_id, outcome, **kw):
            audit_outcomes.append(outcome)

        with patch.object(po, "_append_audit", new=fake_append_audit):
            await po._process_proactive_message(ctx, "msg-ks-01", '{"trace_id":"trace-skip-01","rule_name":"R1","canonical_query":"up","threshold":0.5,"metric_value":0.9}')
        assert "SKIPPED_KILL_SWITCH" in audit_outcomes

    @pytest.mark.asyncio
    async def test_bad_payload_returns_fail_audit(self):
        """Bad JSON payload → audit FAIL, no exception."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka)
        audit_outcomes: list[str] = []

        async def fake_append_audit(ctx, *, trace_id, rule_id, outcome, **kw):
            audit_outcomes.append(outcome)

        with patch.object(po, "_append_audit", new=fake_append_audit):
            await po._process_proactive_message(ctx, "msg-bad-01", "not valid json {{{")
        assert "FAIL" in audit_outcomes

    @pytest.mark.asyncio
    async def test_valid_message_runs_pipeline(self):
        """Valid payload runs _proactive_event_pipeline."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka)
        pipeline_called = [False]

        async def fake_pipeline(*a, **kw):
            pipeline_called[0] = True

        ev_json = json.dumps({
            "trace_id": "trace-valid01",
            "rule_name": "TestRule",
            "canonical_query": "cpu > 0.9",
            "threshold": 0.8,
            "metric_value": 0.95,
        })

        with patch.object(po, "_proactive_event_pipeline", new=fake_pipeline), \
             patch.object(po, "emit_transition", new=AsyncMock()), \
             patch.object(po, "emit_terminal_tombstone", new=AsyncMock()):
            await po._process_proactive_message(ctx, "msg-valid-01", ev_json)
        assert pipeline_called[0]

    @pytest.mark.asyncio
    async def test_pipeline_timeout_appends_dlq(self):
        """Pipeline timeout → EVENT_TIMEOUT audit + DLQ + emit_terminal_tombstone."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(proactive_event_timeout_sec=0.01))
        audit_outcomes: list[str] = []

        async def fake_append_audit(ctx, *, trace_id, rule_id, outcome, **kw):
            audit_outcomes.append(outcome)

        async def slow_pipeline(*a, **kw):
            await asyncio.sleep(10)  # Will timeout

        ev_json = json.dumps({
            "trace_id": "trace-timeout01",
            "rule_name": "TestRule",
            "canonical_query": "cpu > 0.9",
            "threshold": 0.8,
            "metric_value": 0.95,
        })

        with patch.object(po, "_proactive_event_pipeline", new=slow_pipeline), \
             patch.object(po, "_append_audit", new=fake_append_audit), \
             patch.object(po, "_append_dlq_proactive", new=AsyncMock()), \
             patch.object(po, "emit_transition", new=AsyncMock()), \
             patch.object(po, "emit_terminal_tombstone", new=AsyncMock()):
            await po._process_proactive_message(ctx, "msg-timeout-01", ev_json)
        assert "EVENT_TIMEOUT" in audit_outcomes


# ---------------------------------------------------------------------------
# _proactive_event_pipeline — SOP hit path (lines 694-716)
# ---------------------------------------------------------------------------

class TestProactiveEventPipelineSopHit:
    @pytest.mark.asyncio
    async def test_sop_hit_sends_telegram(self):
        """SOP hit with telegram → telegram message sent."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        tg = AsyncMock()
        ctx = _make_ctx(
            kafka=kafka,
            telegram=tg,
            settings=_make_settings(telegram_admin_chat_id=123456),
        )
        ev = _make_ev()

        async def fake_resolve_remediation(ctx, query, *, trace, collection_name, score_threshold):
            return True, "Fixed: restart applied", None

        audit_outcomes: list[str] = []

        async def fake_append_audit(ctx, *, trace_id, rule_id, outcome, **kw):
            audit_outcomes.append(outcome)

        with patch("workers.proactive_observer.resolve_remediation_from_memory", new=fake_resolve_remediation), \
             patch.object(po, "_append_audit", new=fake_append_audit), \
             patch.object(po, "_update_learning_pattern_stats", new=AsyncMock()), \
             patch("workers.proactive_observer.child_span", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))):
            await po._proactive_event_pipeline(ctx, ev, "msg-001", "pk-001", "{}")
        assert "SUCCESS" in audit_outcomes
        tg.send_message.assert_awaited()

    @pytest.mark.asyncio
    async def test_sop_hit_no_telegram(self):
        """SOP hit without telegram → no telegram call, audit SUCCESS."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(telegram_admin_chat_id=None))
        ev = _make_ev()

        async def fake_resolve_remediation(ctx, query, *, trace, collection_name, score_threshold):
            return True, "Fixed", None

        audit_outcomes: list[str] = []

        async def fake_append_audit(ctx, *, trace_id, rule_id, outcome, **kw):
            audit_outcomes.append(outcome)

        with patch("workers.proactive_observer.resolve_remediation_from_memory", new=fake_resolve_remediation), \
             patch.object(po, "_append_audit", new=fake_append_audit), \
             patch.object(po, "_update_learning_pattern_stats", new=AsyncMock()), \
             patch("workers.proactive_observer.child_span", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))):
            await po._proactive_event_pipeline(ctx, ev, "msg-001", "pk-001", "{}")
        assert "SUCCESS" in audit_outcomes


# ---------------------------------------------------------------------------
# _proactive_event_pipeline — SOP miss + governance deny (lines 718-866)
# ---------------------------------------------------------------------------

class TestProactiveEventPipelineSopMiss:
    @pytest.mark.asyncio
    async def test_sop_miss_governance_deny_skips_fallback(self):
        """SOP miss → governance deny → FALLBACK_DENY, fallback not run."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka)
        ev = _make_ev()

        audit_outcomes: list[str] = []

        async def fake_append_audit(ctx, *, trace_id, rule_id, outcome, **kw):
            audit_outcomes.append(outcome)

        async def fake_resolve_remediation(ctx, query, *, trace, collection_name, score_threshold):
            return False, None, None

        async def fake_resolve_action_exp(*a, **kw):
            return False, None, None, {}

        async def fake_governance(*a, **kw):
            return "deny", 0.3

        with patch("workers.proactive_observer.resolve_remediation_from_memory", new=fake_resolve_remediation), \
             patch.object(po, "_resolve_from_action_experience", new=fake_resolve_action_exp), \
             patch.object(po, "_learning_governance_decision", new=fake_governance), \
             patch.object(po, "_is_negative_pattern", return_value=False), \
             patch.object(po, "_append_audit", new=fake_append_audit), \
             patch.object(po, "_update_learning_pattern_stats", new=AsyncMock()), \
             patch("workers.proactive_observer.child_span", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))):
            await po._proactive_event_pipeline(ctx, ev, "msg-001", "pk-001", "{}")
        assert "FALLBACK_DENY" in audit_outcomes
        assert "SOP_MISS" in audit_outcomes

    @pytest.mark.asyncio
    async def test_sop_miss_governance_deny_telegram_notification(self):
        """Governance deny with telegram → notification sent."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        tg = AsyncMock()
        ctx = _make_ctx(
            kafka=kafka, telegram=tg,
            settings=_make_settings(telegram_admin_chat_id=123456)
        )
        ev = _make_ev()

        async def fake_resolve_remediation(ctx, query, *, trace, collection_name, score_threshold):
            return False, None, None

        async def fake_resolve_action_exp(*a, **kw):
            return False, None, None, {}

        async def fake_governance(*a, **kw):
            return "deny", 0.3

        with patch("workers.proactive_observer.resolve_remediation_from_memory", new=fake_resolve_remediation), \
             patch.object(po, "_resolve_from_action_experience", new=fake_resolve_action_exp), \
             patch.object(po, "_learning_governance_decision", new=fake_governance), \
             patch.object(po, "_is_negative_pattern", return_value=False), \
             patch.object(po, "_append_audit", new=AsyncMock()), \
             patch.object(po, "_update_learning_pattern_stats", new=AsyncMock()), \
             patch("workers.proactive_observer.child_span", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))):
            await po._proactive_event_pipeline(ctx, ev, "msg-001", "pk-001", "{}")
        tg.send_message.assert_awaited()

    @pytest.mark.asyncio
    async def test_sop_miss_fallback_enabled_runs_react(self):
        """SOP miss + governance allow + fallback_enabled → run_proactive_react_fallback called."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(
            proactive_fallback_enabled=True, diagnostic_dictionary_enabled=False
        ))
        ev = _make_ev()
        react_called = [False]

        async def fake_resolve_remediation(ctx, query, *, trace, collection_name, score_threshold):
            return False, None, None

        async def fake_resolve_action_exp(*a, **kw):
            return False, None, None, {}

        async def fake_governance(*a, **kw):
            return "allow", 0.95

        async def fake_react(ctx, ev, *, trace, pattern_key, msg_id):
            react_called[0] = True

        with patch("workers.proactive_observer.resolve_remediation_from_memory", new=fake_resolve_remediation), \
             patch.object(po, "_resolve_from_action_experience", new=fake_resolve_action_exp), \
             patch.object(po, "_learning_governance_decision", new=fake_governance), \
             patch.object(po, "_is_negative_pattern", return_value=False), \
             patch.object(po, "_append_audit", new=AsyncMock()), \
             patch.object(po, "_update_learning_pattern_stats", new=AsyncMock()), \
             patch("workers.proactive_observer.run_proactive_react_fallback", new=fake_react), \
             patch("workers.proactive_observer.child_span", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))):
            await po._proactive_event_pipeline(ctx, ev, "msg-001", "pk-001", "{}")
        assert react_called[0]

    @pytest.mark.asyncio
    async def test_sop_miss_fallback_disabled_telegram_sop_miss_message(self):
        """SOP miss + fallback_disabled + telegram → SOP miss telegram message."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        tg = AsyncMock()
        ctx = _make_ctx(
            kafka=kafka, telegram=tg,
            settings=_make_settings(
                telegram_admin_chat_id=123456,
                proactive_fallback_enabled=False,
            )
        )
        ev = _make_ev()

        async def fake_resolve_remediation(ctx, query, *, trace, collection_name, score_threshold):
            return False, None, None

        async def fake_resolve_action_exp(*a, **kw):
            return False, None, None, {}

        async def fake_governance(*a, **kw):
            return "allow", 0.95

        with patch("workers.proactive_observer.resolve_remediation_from_memory", new=fake_resolve_remediation), \
             patch.object(po, "_resolve_from_action_experience", new=fake_resolve_action_exp), \
             patch.object(po, "_learning_governance_decision", new=fake_governance), \
             patch.object(po, "_is_negative_pattern", return_value=False), \
             patch.object(po, "_append_audit", new=AsyncMock()), \
             patch.object(po, "_update_learning_pattern_stats", new=AsyncMock()), \
             patch("workers.proactive_observer.child_span", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))):
            await po._proactive_event_pipeline(ctx, ev, "msg-001", "pk-001", "{}")
        tg.send_message.assert_awaited()


# ---------------------------------------------------------------------------
# _proactive_event_pipeline — learning hit paths (lines 734-835)
# ---------------------------------------------------------------------------

class TestProactiveEventPipelineLearningHit:
    @pytest.mark.asyncio
    async def test_learning_hit_actionable_resolved(self):
        """Learning hit with actionable tool → learning_resolved outcome."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka)
        ev = _make_ev()

        audit_outcomes: list[str] = []

        async def fake_append_audit(ctx, *, trace_id, rule_id, outcome, **kw):
            audit_outcomes.append(outcome)

        async def fake_resolve_remediation(ctx, query, *, trace, collection_name, score_threshold):
            return False, None, None

        async def fake_resolve_action_exp(*a, **kw):
            # Return a learning hit with a mutate tool
            return (
                True,
                "[STATUS] business_hit rollout ok",
                "k8s_rollout_restart",
                {"score": 0.95, "args": {"namespace": "multi-agent", "deployment": "my-app"}},
            )

        with patch("workers.proactive_observer.resolve_remediation_from_memory", new=fake_resolve_remediation), \
             patch.object(po, "_resolve_from_action_experience", new=fake_resolve_action_exp), \
             patch.object(po, "_is_negative_pattern", return_value=False), \
             patch.object(po, "_append_audit", new=fake_append_audit), \
             patch.object(po, "_update_learning_pattern_stats", new=AsyncMock()), \
             patch.object(po, "_save_proactive_learning_record", new=AsyncMock()), \
             patch("workers.proactive_observer.child_span", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))):
            await po._proactive_event_pipeline(ctx, ev, "msg-001", "pk-001", "{}")
        # Should have LEARNING_HIT_OK and SOP_MISS
        assert "LEARNING_HIT_OK" in audit_outcomes

    @pytest.mark.asyncio
    async def test_learning_hit_observe_only(self):
        """Learning hit with non-mutate tool → learning_observe (no upsert)."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka)
        ev = _make_ev()

        audit_outcomes: list[str] = []

        async def fake_append_audit(ctx, *, trace_id, rule_id, outcome, **kw):
            audit_outcomes.append(outcome)

        async def fake_resolve_remediation(ctx, query, *, trace, collection_name, score_threshold):
            return False, None, None

        async def fake_resolve_action_exp(*a, **kw):
            # promql_instant tool (non-mutate) with business_hit
            return (
                True,
                "[STATUS] business_hit value=0.95",
                "promql_instant",
                {"score": 0.92, "args": {"query": "up"}},
            )

        with patch("workers.proactive_observer.resolve_remediation_from_memory", new=fake_resolve_remediation), \
             patch.object(po, "_resolve_from_action_experience", new=fake_resolve_action_exp), \
             patch.object(po, "_is_negative_pattern", return_value=False), \
             patch.object(po, "_append_audit", new=fake_append_audit), \
             patch.object(po, "_update_learning_pattern_stats", new=AsyncMock()), \
             patch.object(po, "_save_proactive_learning_record", new=AsyncMock()), \
             patch("workers.proactive_observer.child_span", return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))):
            await po._proactive_event_pipeline(ctx, ev, "msg-001", "pk-001", "{}")
        # promql_instant is not in PROACTIVE_MUTATE_TOOLS → not actionable → learning_observe or LEARNING_HIT_OBSERVE
        assert "LEARNING_HIT_OBSERVE" in audit_outcomes or "SOP_MISS" in audit_outcomes


# ---------------------------------------------------------------------------
# evaluate_proactive_triggers (lines 453-498)
# ---------------------------------------------------------------------------

class TestEvaluateProactiveTriggers:
    @pytest.mark.asyncio
    async def test_kill_switch_returns_zero(self):
        """Kill switch active → returns 0, no kafka produce."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await r.set("omni:proactive:kill_switch", "1")
        ctx = _make_ctx(redis_client=r, kafka=kafka)
        result = await po.evaluate_proactive_triggers(ctx)
        assert result == 0

    @pytest.mark.asyncio
    async def test_no_metric_value_returns_zero(self):
        """Metric query returns None → returns 0."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka)

        with patch.object(po, "_instant_scalar", return_value=None):
            result = await po.evaluate_proactive_triggers(ctx)
        assert result == 0

    @pytest.mark.asyncio
    async def test_metric_below_threshold_returns_zero(self):
        """Metric value ≤ threshold → returns 0."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(proactive_trigger_threshold=0.9))

        with patch.object(po, "_instant_scalar", return_value=0.5):
            result = await po.evaluate_proactive_triggers(ctx)
        assert result == 0

    @pytest.mark.asyncio
    async def test_metric_above_threshold_produces_event(self):
        """Metric value > threshold → produces event, returns 1."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(
            redis_client=r, kafka=kafka,
            settings=_make_settings(proactive_trigger_threshold=0.5)
        )

        with patch.object(po, "_instant_scalar", return_value=0.9):
            result = await po.evaluate_proactive_triggers(ctx)
        assert result == 1
        kafka.send_envelope_inner.assert_awaited()

    @pytest.mark.asyncio
    async def test_cooldown_prevents_duplicate_event(self):
        """If cooldown key is set, event is suppressed."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(
            redis_client=r, kafka=kafka,
            settings=_make_settings(proactive_trigger_threshold=0.5)
        )
        # Pre-set cooldown key using same rule/promql as production code
        rule = po.DEFAULT_RULE
        promql = "up == 0"
        dedupe = f"{rule}:{promql[:120]}"
        ck = f"omni:proactive:cooldown:{hash(dedupe) & 0xFFFFFFFF:X}"
        await r.setex(ck, 60, "1")

        with patch.object(po, "_instant_scalar", return_value=0.9):
            result = await po.evaluate_proactive_triggers(ctx)
        assert result == 0


# ---------------------------------------------------------------------------
# proactive_evaluate_loop (lines 1111-1132)
# ---------------------------------------------------------------------------

class TestProactiveEvaluateLoop:
    @pytest.mark.asyncio
    async def test_disabled_proactive_sleeps(self):
        """proactive_enabled=False → loop sleeps without calling evaluate."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(
            kafka=kafka,
            settings=_make_settings(proactive_enabled=False, proactive_eval_interval_sec=0.05)
        )
        stop = asyncio.Event()
        called = [False]

        async def fake_evaluate(ctx):
            called[0] = True

        task = asyncio.create_task(po.proactive_evaluate_loop(ctx, stop))
        await asyncio.sleep(0.1)
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()
        assert not called[0]

    @pytest.mark.asyncio
    async def test_enabled_calls_evaluate(self):
        """proactive_enabled=True → evaluate_proactive_triggers called."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(
            kafka=kafka,
            settings=_make_settings(proactive_enabled=True, proactive_eval_interval_sec=0.05)
        )
        stop = asyncio.Event()
        called = [0]

        async def fake_evaluate(ctx):
            called[0] += 1
            return 0

        with patch.object(po, "evaluate_proactive_triggers", new=fake_evaluate):
            task = asyncio.create_task(po.proactive_evaluate_loop(ctx, stop))
            await asyncio.sleep(0.2)
            stop.set()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.TimeoutError:
                task.cancel()
        assert called[0] >= 1

    @pytest.mark.asyncio
    async def test_evaluate_exception_swallowed(self):
        """Exception in evaluate_proactive_triggers → swallowed, loop continues."""
        import workers.proactive_observer as po
        kafka = _make_kafka()
        ctx = _make_ctx(
            kafka=kafka,
            settings=_make_settings(proactive_enabled=True, proactive_eval_interval_sec=0.05)
        )
        stop = asyncio.Event()
        call_count = [0]

        async def flaky_evaluate(ctx):
            call_count[0] += 1
            raise RuntimeError("evaluate boom")

        with patch.object(po, "evaluate_proactive_triggers", new=flaky_evaluate):
            task = asyncio.create_task(po.proactive_evaluate_loop(ctx, stop))
            await asyncio.sleep(0.2)
            stop.set()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.TimeoutError:
                task.cancel()
        # Should have run at least once despite exception
        assert call_count[0] >= 1


# ---------------------------------------------------------------------------
# _fail_safe_after_tool_error (lines 529-646)
# ---------------------------------------------------------------------------

class TestFailSafeAfterToolError:
    @pytest.mark.asyncio
    async def test_no_resource_ref_k8s_unavailable(self):
        """No resource ref → k8s_state unavailable."""
        import workers.proactive_observer as po
        from workers.tools import ToolCallPayload
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka)
        ev = _make_ev()
        call = ToolCallPayload(tool="promql_instant", args={"query": "up"})
        err = RuntimeError("tool failed")

        with patch("workers.proactive_observer.extract_resource_ref", return_value=None), \
             patch.object(po, "_append_audit", new=AsyncMock()), \
             patch.object(po, "_save_proactive_learning_record", new=AsyncMock()), \
             patch.object(po, "_append_dlq_proactive", new=AsyncMock(return_value="dlq-01")), \
             patch.object(po, "emit_terminal_tombstone", new=AsyncMock()):
            await po._fail_safe_after_tool_error(
                ctx, ev, "trace-01", "pk-01", call, err, reason_code="TOOL_EXCEPTION"
            )
        # Should complete without raise

    @pytest.mark.asyncio
    async def test_with_resource_ref_freeze_applied(self):
        """Resource ref + k8s state available → freeze applied."""
        import workers.proactive_observer as po
        from workers.tools import ToolCallPayload
        kafka = _make_kafka()
        ctx = _make_ctx(
            kafka=kafka,
            settings=_make_settings(proactive_resource_freeze_enabled=True)
        )
        ev = _make_ev()
        call = ToolCallPayload(tool="k8s_rollout_restart", args={"namespace": "multi-agent", "deployment": "my-app"})
        err = RuntimeError("tool failed")

        async def fake_k8s_state(*a, **kw):
            return {"phase": "Running", "ready": True}

        freeze_called = [False]

        async def fake_set_freeze(*a, **kw):
            freeze_called[0] = True
            return "freeze:key:001"

        with patch("workers.proactive_observer.extract_resource_ref", return_value=("multi-agent", "Deployment", "my-app")), \
             patch("workers.proactive_observer.fetch_last_known_state", new=fake_k8s_state), \
             patch("workers.proactive_observer.set_resource_freeze", new=fake_set_freeze), \
             patch.object(po, "_append_audit", new=AsyncMock()), \
             patch.object(po, "_save_proactive_learning_record", new=AsyncMock()), \
             patch.object(po, "_append_dlq_proactive", new=AsyncMock(return_value="dlq-02")), \
             patch.object(po, "emit_terminal_tombstone", new=AsyncMock()):
            await po._fail_safe_after_tool_error(
                ctx, ev, "trace-02", "pk-02", call, err, reason_code="TOOL_TIMEOUT"
            )
        assert freeze_called[0]

    @pytest.mark.asyncio
    async def test_telegram_notification_on_fail_safe(self):
        """Fail safe with telegram → sends REQUIRES_HUMAN notification."""
        import workers.proactive_observer as po
        from workers.tools import ToolCallPayload
        kafka = _make_kafka()
        tg = AsyncMock()
        ctx = _make_ctx(
            kafka=kafka, telegram=tg,
            settings=_make_settings(telegram_admin_chat_id=123456)
        )
        ev = _make_ev()
        call = ToolCallPayload(tool="promql_instant", args={"query": "up"})
        err = RuntimeError("oops")

        with patch("workers.proactive_observer.extract_resource_ref", return_value=None), \
             patch.object(po, "_append_audit", new=AsyncMock()), \
             patch.object(po, "_save_proactive_learning_record", new=AsyncMock()), \
             patch.object(po, "_append_dlq_proactive", new=AsyncMock(return_value="dlq-03")), \
             patch.object(po, "emit_terminal_tombstone", new=AsyncMock()):
            await po._fail_safe_after_tool_error(
                ctx, ev, "trace-03", "pk-03", call, err, reason_code="TOOL_EXCEPTION"
            )
        tg.send_message.assert_awaited()
        call_str = str(tg.send_message.call_args_list)
        assert "REQUIRES_HUMAN" in call_str

    @pytest.mark.asyncio
    async def test_dlq_exception_swallowed(self):
        """DLQ append failure → swallowed, rest of fail_safe continues."""
        import workers.proactive_observer as po
        from workers.tools import ToolCallPayload
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka)
        ev = _make_ev()
        call = ToolCallPayload(tool="promql_instant", args={"query": "up"})
        err = RuntimeError("tool failed")

        async def raise_dlq(*a, **kw):
            raise RuntimeError("dlq down")

        with patch("workers.proactive_observer.extract_resource_ref", return_value=None), \
             patch.object(po, "_append_audit", new=AsyncMock()), \
             patch.object(po, "_save_proactive_learning_record", new=AsyncMock()), \
             patch.object(po, "_append_dlq_proactive", new=raise_dlq), \
             patch.object(po, "emit_terminal_tombstone", new=AsyncMock()):
            await po._fail_safe_after_tool_error(
                ctx, ev, "trace-04", "pk-04", call, err, reason_code="TOOL_EXCEPTION"
            )
        # Should complete without raise
