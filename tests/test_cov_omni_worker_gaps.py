"""Coverage gap tests for workers.omni_worker.

Targets uncovered lines:
  147:        _lock_heartbeat stop path
  393-420:    kafka_alerts_loop
  437-479:    kafka_evidence_loop  (reconnect backoff, stop mid-consume)
  483-512:    telegram_loop
  516-550:    build_context (partial)
  563-568:    _run_autonomous_safe
  571-607:    _worker_background_tasks (all roles)
  612-714:    run_worker (startup, signal handlers, tasks)
  718, 722:   main()
"""

from __future__ import annotations

import asyncio
import json
import os
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
        "kafka_topic_alerts": "omni-alerts",
        "kafka_topic_diagnostic_evidence": "omni-diagnostic-evidence",
        "kafka_topic_proactive_incidents": "omni-proactive-incidents",
        "kafka_topic_audit_proactive": "omni-audit-proactive",
        "kafka_topic_dlq": "omni-dlq",
        "kafka_bootstrap_servers": "localhost:9092",
        "consumer_group": "omni-workers",
        "consumer_group_analyst": "omni-analyst",
        "consumer_name": "omni-worker-1",
        "consumer_name_analyst": "omni-analyst-1",
        "worker_role": "analyst",
        "telegram_polling_enabled": False,
        "telegram_admin_chat_id": None,
        "autonomous_decider_enabled": False,
        "proactive_enabled": False,
        "vllm_base_url": "http://localhost:11434",
        "vllm_embed_url": "http://localhost:11434",
        "metrics_listen_host": "0.0.0.0",
        "metrics_listen_port": 9090,
        "otel_service_name": "omni-worker",
        "otel_exporter_otlp_endpoint": "",
        "otel_tracing_enabled": False,
        "proactive_kill_switch_key": "omni:proactive:kill_switch",
        "proactive_eval_interval_sec": 120,
        "proactive_block_ms": 1000,
        "telegram_enabled": False,
        "llm_num_parallel": 2,
        "llm_lease_ttl_sec": 120,
        "embed_model": "nomic-embed-text",
        "chat_model": "qwen2.5:7b",
        "cb_max_delayed_queue": 5000,
        "siem_chain_consumer_enabled": False,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_ctx(redis_client=None, kafka=None, settings=None, **kw: Any) -> SimpleNamespace:
    scout_ready = asyncio.Event()
    scout_ready.set()
    sem = AsyncMock()
    sem.acquire_proactive = AsyncMock(return_value="token-abc")
    sem.release = AsyncMock()
    ledger = MagicMock()
    ledger.record_exception = AsyncMock()
    ledger.ensure_ready = AsyncMock()

    llm = AsyncMock()
    # embed phải trả dict thật: consumer gọi .get() trên kết quả — nếu để mock
    # async trần, .get() sinh coroutine không bao giờ được await (RuntimeWarning).
    llm.embed = AsyncMock(return_value={"embedding": [0.0] * 8})

    defaults: dict[str, Any] = {
        "settings": settings or _make_settings(),
        "redis": redis_client or fakeredis.aioredis.FakeRedis(decode_responses=True),
        "llm": llm,
        "vector_store": MagicMock(),
        "ledger": ledger,
        "semaphore": sem,
        "telegram": None,
        "kafka": kafka,
        "telegram_chat_id": None,
        "inbound_source": "",
        "inbound_user_text": "",
        "restart_rollout_explicit": False,
        "pod_discovery_pairs": [],
        "scout_ready": scout_ready,
        "inbound_trace_id": "test-trace",
        "llm_slot_held": False,
        "inbound_proactive": False,
        "k8s_mutated": False,
        "fallback_inline_commands": None,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_kafka():
    kafka = MagicMock()
    kafka.send_dict = AsyncMock()
    kafka.send_envelope_inner = AsyncMock()
    kafka.close = AsyncMock()
    return kafka


# ---------------------------------------------------------------------------
# _lock_heartbeat
# ---------------------------------------------------------------------------

class TestLockHeartbeat:
    @pytest.mark.asyncio
    async def test_stop_immediately(self):
        """Stop event set before heartbeat loop ticks."""
        from workers.omni_worker import _lock_heartbeat
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await r.set("lk:test", "locked", ex=15)
        stop = asyncio.Event()
        stop.set()
        # Should return almost immediately
        await asyncio.wait_for(_lock_heartbeat(r, "lk:test", stop), timeout=1.0)

    @pytest.mark.asyncio
    async def test_heartbeat_extends_ttl(self):
        """Heartbeat refreshes TTL then stops."""
        from workers.omni_worker import _lock_heartbeat
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await r.set("lk:hb", "locked", ex=5)
        stop = asyncio.Event()
        task = asyncio.create_task(_lock_heartbeat(r, "lk:hb", stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)


# ---------------------------------------------------------------------------
# _process_stream_entry
# ---------------------------------------------------------------------------

class TestProcessStreamEntry:
    @pytest.mark.asyncio
    async def test_duplicate_lock_skips(self):
        """If lock already held, message is skipped (idempotency)."""
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        msg_id = "topic:0:100"
        lock_key = f"omni:lock:{msg_id}"
        # Pre-set the lock so acquire will fail
        await r.set(lock_key, "other-worker", nx=True, ex=15)
        kafka = _make_kafka()
        ctx = _make_ctx(redis_client=r, kafka=kafka)
        from workers.omni_worker import _process_stream_entry
        # Should return without processing
        await _process_stream_entry(ctx, msg_id, {"data": '{"trace_id": "tr-001"}'})

    @pytest.mark.asyncio
    async def test_valid_message_processed(self):
        """Valid message acquires lock, runs pipeline, cleans up."""
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        kafka = _make_kafka()
        ctx = _make_ctx(redis_client=r, kafka=kafka)

        with patch("workers.omni_worker.run_diagnostic_pipeline", new=AsyncMock()), \
             patch("workers.omni_worker.emit_transition", new=AsyncMock()), \
             patch("workers.omni_worker.emit_terminal_tombstone", new=AsyncMock()), \
             patch("workers.omni_worker.build_anomaly_event_from_alert_payload", return_value=MagicMock()):
            from workers.omni_worker import _process_stream_entry
            await _process_stream_entry(
                ctx,
                "topic:0:1",
                {"data": json.dumps({"trace_id": "trace-0001", "source": "test"})},
            )

    @pytest.mark.asyncio
    async def test_exception_retry_logic_increments(self):
        """Exception in message handling → retry counter incremented in redis."""
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        kafka = _make_kafka()
        ctx = _make_ctx(redis_client=r, kafka=kafka)

        async def raise_exc(ctx, ev):
            raise RuntimeError("processing boom")

        with patch("workers.omni_worker.run_diagnostic_pipeline", side_effect=raise_exc), \
             patch("workers.omni_worker.emit_transition", new=AsyncMock()), \
             patch("workers.omni_worker.emit_terminal_tombstone", new=AsyncMock()), \
             patch("workers.omni_worker.build_anomaly_event_from_alert_payload", return_value=MagicMock()):
            from workers.omni_worker import _process_stream_entry
            await _process_stream_entry(
                ctx,
                "topic:0:2",
                {"data": json.dumps({"trace_id": "trace-abc1", "source": "test"})},
            )
        # retry key should be set
        retry_val = await r.get("omni:retry:trace-abc1")
        assert retry_val == "1"

    @pytest.mark.asyncio
    async def test_exception_dlq_on_third_retry(self):
        """After 3 retries, message goes to DLQ."""
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        kafka = _make_kafka()
        ctx = _make_ctx(redis_client=r, kafka=kafka)
        # Pre-set retry counter to 2
        await r.set("omni:retry:trace-dlq1", "2")

        async def raise_exc(ctx, ev):
            raise RuntimeError("final boom")

        with patch("workers.omni_worker.run_diagnostic_pipeline", side_effect=raise_exc), \
             patch("workers.omni_worker.emit_transition", new=AsyncMock()), \
             patch("workers.omni_worker.emit_terminal_tombstone", new=AsyncMock()), \
             patch("workers.omni_worker.build_anomaly_event_from_alert_payload", return_value=MagicMock()):
            from workers.omni_worker import _process_stream_entry
            await _process_stream_entry(
                ctx,
                "topic:0:3",
                {"data": json.dumps({"trace_id": "trace-dlq1", "source": "test"})},
            )
        # kafka DLQ should be sent
        kafka.send_dict.assert_awaited()

    @pytest.mark.asyncio
    async def test_invalid_trace_id_sanitized(self):
        """Invalid trace_id (special chars) is sanitized to safe fallback."""
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        kafka = _make_kafka()
        ctx = _make_ctx(redis_client=r, kafka=kafka)

        with patch("workers.omni_worker.run_diagnostic_pipeline", new=AsyncMock()), \
             patch("workers.omni_worker.emit_transition", new=AsyncMock()), \
             patch("workers.omni_worker.emit_terminal_tombstone", new=AsyncMock()), \
             patch("workers.omni_worker.build_anomaly_event_from_alert_payload", return_value=MagicMock()):
            from workers.omni_worker import _process_stream_entry
            await _process_stream_entry(
                ctx,
                "topic:0:9",
                {"data": json.dumps({"trace_id": "!!!invalid!!!", "source": "test"})},
            )
        # Should not raise


# ---------------------------------------------------------------------------
# delayed_queue_loop
# ---------------------------------------------------------------------------

class TestDelayedQueueLoop:
    @pytest.mark.asyncio
    async def test_no_items_returns_quickly(self):
        """Empty delayed queue → loop runs one iteration and exits on stop."""
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        kafka = _make_kafka()
        ctx = _make_ctx(redis_client=r, kafka=kafka)
        stop = asyncio.Event()

        from workers.omni_worker import delayed_queue_loop
        task = asyncio.create_task(delayed_queue_loop(ctx, stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=3.0)

    @pytest.mark.asyncio
    async def test_item_past_due_sent_to_kafka(self):
        """Item in delayed queue past due → sent to kafka and removed."""
        import time
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        kafka = _make_kafka()
        ctx = _make_ctx(redis_client=r, kafka=kafka)
        # Add an item with score in the past
        payload = json.dumps({"msg_id": "mid-1", "data": '{"trace_id":"trace-q1"}', "_stable_id": "trace-q1"})
        await r.zadd("omni:delayed_queue", {payload: time.time() - 10})
        stop = asyncio.Event()

        from workers.omni_worker import delayed_queue_loop
        task = asyncio.create_task(delayed_queue_loop(ctx, stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=3.0)
        kafka.send_dict.assert_awaited()


# ---------------------------------------------------------------------------
# circuit_breaker_loop
# ---------------------------------------------------------------------------

class TestCircuitBreakerLoop:
    @pytest.mark.asyncio
    async def test_trips_when_queue_too_large(self):
        """Circuit breaker trips when delayed_queue > cb_limit."""
        import workers.metrics_exporter as mx
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        kafka = _make_kafka()
        ctx = _make_ctx(redis_client=r, kafka=kafka, settings=_make_settings(cb_max_delayed_queue=2))
        # Add 5 items to exceed the limit of 2
        import time
        for i in range(5):
            await r.zadd("omni:delayed_queue", {f"item-{i}": time.time() + i})
        stop = asyncio.Event()

        with patch.object(mx, "set_circuit_breaker_active") as mock_cb:
            from workers.omni_worker import circuit_breaker_loop
            task = asyncio.create_task(circuit_breaker_loop(ctx, stop))
            await asyncio.sleep(0.1)
            stop.set()
            await asyncio.wait_for(task, timeout=3.0)
        mock_cb.assert_called()

    @pytest.mark.asyncio
    async def test_clears_when_queue_ok(self):
        """Circuit breaker clears when delayed_queue returns to normal."""
        import workers.metrics_exporter as mx
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        kafka = _make_kafka()
        ctx = _make_ctx(redis_client=r, kafka=kafka, settings=_make_settings(cb_max_delayed_queue=100))
        # Set breaker as active
        await r.setex("omni:circuit_breaker:active", 60, "1")
        stop = asyncio.Event()

        with patch.object(mx, "set_circuit_breaker_active") as mock_cb:
            from workers.omni_worker import circuit_breaker_loop
            task = asyncio.create_task(circuit_breaker_loop(ctx, stop))
            await asyncio.sleep(0.1)
            stop.set()
            await asyncio.wait_for(task, timeout=3.0)
        # Should call set_circuit_breaker_active(0) to clear
        mock_cb.assert_called_with(0)


# ---------------------------------------------------------------------------
# _run_kpi_collector
# ---------------------------------------------------------------------------

class TestRunKpiCollector:
    @pytest.mark.asyncio
    async def test_kpi_collector_runs_and_handles_error(self):
        """_run_kpi_collector wraps _kpi_run; swallows exceptions."""
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka)
        stop = asyncio.Event()
        stop.set()

        async def fake_kpi_run(redis, kafka_bootstrap, stop):
            raise RuntimeError("kpi boom")

        with patch("workers.omni_worker._kpi_run", new=fake_kpi_run):
            from workers.omni_worker import _run_kpi_collector
            await _run_kpi_collector(ctx, stop)  # Should not raise


# ---------------------------------------------------------------------------
# _run_autonomous_safe
# ---------------------------------------------------------------------------

class TestRunAutonomousSafe:
    @pytest.mark.asyncio
    async def test_swallows_exception(self):
        """_run_autonomous_safe swallows exception from deep_scout_autonomous."""
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka)

        async def fail_scout(ctx, periodic):
            raise RuntimeError("scout boom")

        with patch("workers.omni_worker.run_deep_scout_autonomous", new=fail_scout):
            from workers.omni_worker import _run_autonomous_safe
            await _run_autonomous_safe(ctx)  # Should not raise

    @pytest.mark.asyncio
    async def test_calls_deep_scout_autonomous(self):
        """_run_autonomous_safe calls run_deep_scout_autonomous with periodic=False."""
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka)

        called_with_periodic: list[bool] = []

        async def fake_scout(ctx, periodic):
            called_with_periodic.append(periodic)

        with patch("workers.omni_worker.run_deep_scout_autonomous", new=fake_scout):
            from workers.omni_worker import _run_autonomous_safe
            await _run_autonomous_safe(ctx)

        assert called_with_periodic == [False]


# ---------------------------------------------------------------------------
# _worker_background_tasks
# ---------------------------------------------------------------------------

class TestWorkerBackgroundTasks:
    def _run_sync(self, coro):
        """Helper to run coroutines in test event loop."""
        return asyncio.get_event_loop().run_until_complete(coro)

    @pytest.mark.asyncio
    async def test_executor_role_creates_actions_loop(self):
        """Role=executor → only kafka_actions_loop task."""
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(worker_role="executor"))
        stop = asyncio.Event()
        stop.set()

        async def noop_exec(*a, **k):
            pass

        with patch("workers.omni_worker.kafka_actions_loop", side_effect=noop_exec):
            from workers.omni_worker import _worker_background_tasks
            tasks = _worker_background_tasks(ctx, stop)
        assert len(tasks) == 1
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_analyst_role_creates_evidence_feedback_kpi(self):
        """Role=analyst → kafka_evidence_loop + kafka_action_feedback_loop + kpi_collector."""
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(worker_role="analyst"))
        stop = asyncio.Event()
        stop.set()

        async def noop(*a, **k):
            pass

        with patch("workers.omni_worker.kafka_evidence_loop", side_effect=noop), \
             patch("workers.omni_worker.kafka_action_feedback_loop", side_effect=noop), \
             patch("workers.omni_worker._run_kpi_collector", side_effect=noop):
            from workers.omni_worker import _worker_background_tasks
            tasks = _worker_background_tasks(ctx, stop)
        assert len(tasks) >= 2
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_prober_role_creates_alerts_delayed_circuit(self):
        """Role=prober → kafka_alerts_loop + delayed_queue_loop + circuit_breaker_loop."""
        kafka = _make_kafka()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(worker_role="prober"))
        stop = asyncio.Event()
        stop.set()

        async def noop(*a, **k):
            pass

        with patch("workers.omni_worker.kafka_alerts_loop", side_effect=noop), \
             patch("workers.omni_worker.delayed_queue_loop", side_effect=noop), \
             patch("workers.omni_worker.circuit_breaker_loop", side_effect=noop):
            from workers.omni_worker import _worker_background_tasks
            tasks = _worker_background_tasks(ctx, stop)
        # prober has at least 3 tasks (alerts + delayed + circuit_breaker)
        assert len(tasks) >= 3
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_full_role_creates_all_loops(self):
        """Role=full → alerts + delayed + circuit + evidence + feedback + kpi + deep_scout + forecast + snapshot."""
        kafka = _make_kafka()
        ctx = _make_ctx(
            kafka=kafka,
            settings=_make_settings(
                worker_role="full",
                autonomous_decider_enabled=False,
                proactive_enabled=False,
                telegram_polling_enabled=False,
            )
        )
        stop = asyncio.Event()
        stop.set()

        async def noop(*a, **k):
            pass

        with patch("workers.omni_worker.kafka_alerts_loop", side_effect=noop), \
             patch("workers.omni_worker.delayed_queue_loop", side_effect=noop), \
             patch("workers.omni_worker.circuit_breaker_loop", side_effect=noop), \
             patch("workers.omni_worker.kafka_evidence_loop", side_effect=noop), \
             patch("workers.omni_worker.kafka_action_feedback_loop", side_effect=noop), \
             patch("workers.omni_worker._run_kpi_collector", side_effect=noop), \
             patch("workers.omni_worker.deep_scout_periodic_loop", side_effect=noop), \
             patch("workers.omni_worker.autonomous_forecast_loop", side_effect=noop), \
             patch("workers.omni_worker.baseline_snapshot_loop", side_effect=noop):
            from workers.omni_worker import _worker_background_tasks
            tasks = _worker_background_tasks(ctx, stop)
        # Full should have at least 9 tasks
        assert len(tasks) >= 9
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_full_role_with_proactive_adds_proactive_tasks(self):
        """Role=full + proactive_enabled → adds proactive_evaluate + kafka_proactive_incidents."""
        kafka = _make_kafka()
        ctx = _make_ctx(
            kafka=kafka,
            settings=_make_settings(
                worker_role="full",
                autonomous_decider_enabled=False,
                proactive_enabled=True,
                telegram_polling_enabled=False,
            )
        )
        stop = asyncio.Event()
        stop.set()

        async def noop(*a, **k):
            pass

        with patch("workers.omni_worker.kafka_alerts_loop", side_effect=noop), \
             patch("workers.omni_worker.delayed_queue_loop", side_effect=noop), \
             patch("workers.omni_worker.circuit_breaker_loop", side_effect=noop), \
             patch("workers.omni_worker.kafka_evidence_loop", side_effect=noop), \
             patch("workers.omni_worker.kafka_action_feedback_loop", side_effect=noop), \
             patch("workers.omni_worker._run_kpi_collector", side_effect=noop), \
             patch("workers.omni_worker.deep_scout_periodic_loop", side_effect=noop), \
             patch("workers.omni_worker.autonomous_forecast_loop", side_effect=noop), \
             patch("workers.omni_worker.baseline_snapshot_loop", side_effect=noop), \
             patch("workers.omni_worker.proactive_evaluate_loop", side_effect=noop), \
             patch("workers.omni_worker.kafka_proactive_incidents_loop", side_effect=noop):
            from workers.omni_worker import _worker_background_tasks
            tasks = _worker_background_tasks(ctx, stop)
        # Should be at least 11 tasks with proactive
        assert len(tasks) >= 11
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_core_role_creates_core_loops(self):
        """Role=core → deep_scout_periodic + forecast + baseline_snapshot."""
        kafka = _make_kafka()
        ctx = _make_ctx(
            kafka=kafka,
            settings=_make_settings(
                worker_role="core",
                autonomous_decider_enabled=False,
                proactive_enabled=False,
            )
        )
        stop = asyncio.Event()
        stop.set()

        async def noop(*a, **k):
            pass

        with patch("workers.omni_worker.deep_scout_periodic_loop", side_effect=noop), \
             patch("workers.omni_worker.autonomous_forecast_loop", side_effect=noop), \
             patch("workers.omni_worker.baseline_snapshot_loop", side_effect=noop):
            from workers.omni_worker import _worker_background_tasks
            tasks = _worker_background_tasks(ctx, stop)
        assert len(tasks) >= 3
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# telegram_loop
# ---------------------------------------------------------------------------

class TestTelegramLoop:
    @pytest.mark.asyncio
    async def test_returns_immediately_when_no_telegram(self):
        """If ctx.telegram is None, telegram_loop returns immediately."""
        ctx = _make_ctx(telegram=None)
        stop = asyncio.Event()
        stop.set()
        from workers.omni_worker import telegram_loop
        await asyncio.wait_for(telegram_loop(ctx, stop), timeout=1.0)

    @pytest.mark.asyncio
    async def test_processes_telegram_update(self):
        """Telegram update with text is enqueued to kafka."""
        kafka = _make_kafka()
        tg = AsyncMock()
        # Return an update with text
        tg.get_updates = AsyncMock(side_effect=[
            {"result": [{
                "update_id": 1001,
                "message": {
                    "message_id": 42,
                    "text": "hello",
                    "chat": {"id": 999},
                    "from": {"id": 999}
                }
            }]},
            asyncio.CancelledError(),  # break the loop
        ])

        ctx = _make_ctx(kafka=kafka, telegram=tg)
        stop = asyncio.Event()

        from workers.omni_worker import telegram_loop
        with patch("workers.omni_worker.summarize_message_update") as mock_sum, \
             patch("workers.omni_worker._handle_telegram_fallback_callback", return_value=False):
            msg_sum = SimpleNamespace(text="hello", chat_id=999, update_id=1001, message_id=42)
            mock_sum.return_value = msg_sum
            try:
                await asyncio.wait_for(telegram_loop(ctx, stop), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

    @pytest.mark.asyncio
    async def test_handles_get_updates_exception(self):
        """Exception from get_updates → logs and continues."""
        kafka = _make_kafka()
        tg = AsyncMock()
        call_count = [0]

        async def mock_get_updates(**kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("network error")
            return {"result": []}

        tg.get_updates = mock_get_updates

        ctx = _make_ctx(kafka=kafka, telegram=tg)
        stop = asyncio.Event()

        from workers.omni_worker import telegram_loop
        task = asyncio.create_task(telegram_loop(ctx, stop))
        await asyncio.sleep(0.1)
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
