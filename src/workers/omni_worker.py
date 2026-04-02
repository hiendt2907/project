"""Omni-Worker: Kafka consumers (alerts + proactive) + (tuỳ chọn) telegram_polling; SIGTERM an toàn."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from typing import Any

import redis.asyncio as redis
from init.deep_scout import DeepScoutSummary, deep_scout_periodic_loop, run_deep_scout
from init.deep_scout_autonomous import run_deep_scout_autonomous
from ingest.telegram import TelegramBotSettings, TelegramClient, summarize_message_update
from llm.ollama_client import OllamaClient
from rag.error_ledger import ErrorLedger
from rag.pgvector_store import (
    init_pg_pool, 
    PGVectorStore, 
    PostgresRAGSettings
)
from workers.autonomous_decider import autonomous_decider_loop
from workers.baseline_snapshot import baseline_snapshot_loop
from workers.forecast_autonomous_loop import autonomous_forecast_loop
from workers.alert_to_event import build_anomaly_event_from_alert_payload
from workers.diagnostic_dispatcher import run_diagnostic_pipeline
from workers.evidence_consumer import reason_from_diagnostic_evidence
from workers.handler_context import WorkerHandlerContext
from workers.handlers import handle_inbound_payload
from workers.kafka_actions_consumer import kafka_actions_loop
from messaging.kafka_bus import KafkaBus, create_producer, decode_kafka_value_to_fields, kafka_msg_id
from workers.proactive_observer import kafka_proactive_incidents_loop, proactive_evaluate_loop
from workers.request_trace import log_end_request, log_start_request
from workers.metrics_exporter import (
    observability_metrics_loop,
    set_last_scout_timestamp,
    start_prometheus_server,
)
from workers.otel_tracing import setup_otel_tracing, shutdown_otel_tracing
from workers.ollama_semaphore import RedisOllamaSemaphore
from workers.settings import WorkerSettings
from workers.redis_client import connect_redis
from workers.telegram_outbound import send_telegram_out_for_inbound

logger = logging.getLogger(__name__)

_send_telegram_out = send_telegram_out_for_inbound


def _redis_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)


async def _handle_telegram_fallback_callback(ctx: WorkerHandlerContext, u: dict[str, Any]) -> bool:
    """Inline keyboard ofs:hash:idx — gửi lệnh vào stream như tin nhắn mới."""
    cb = u.get("callback_query")
    if not isinstance(cb, dict):
        return False
    data = (cb.get("data") or "").strip()
    if not data.startswith("ofs:"):
        return False
    parts = data.split(":")
    cq_id = str(cb.get("id") or "")
    if len(parts) != 3 or not parts[2].isdigit():
        if ctx.telegram and cq_id:
            await ctx.telegram.answer_callback_query(cq_id, text="Nút không hợp lệ")
        return True
    h, idx = parts[1], int(parts[2])
    try:
        trace_stored = _redis_str(await ctx.redis.get(f"omni:fb_h:{h}"))
        if not trace_stored:
            if ctx.telegram and cq_id:
                await ctx.telegram.answer_callback_query(cq_id, text="Hết hạn — gõ lại lệnh")
            return True
        raw_cmds = _redis_str(await ctx.redis.get(f"omni:fb_suggest:{trace_stored}"))
        if not raw_cmds:
            if ctx.telegram and cq_id:
                await ctx.telegram.answer_callback_query(cq_id, text="Hết hạn")
            return True
        cmds = json.loads(raw_cmds)
        if not isinstance(cmds, list) or idx < 0 or idx >= len(cmds):
            if ctx.telegram and cq_id:
                await ctx.telegram.answer_callback_query(cq_id, text="Lỗi nút")
            return True
        text = str(cmds[idx])
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = int(chat.get("id") or 0)
        if ctx.telegram and cq_id:
            await ctx.telegram.answer_callback_query(cq_id)
        trace_id = f"tg-{chat_id}-{u['update_id']}-cb{idx}"
        payload = {
            "text": text,
            "chat_id": chat_id,
            "source": "telegram_callback",
            "update_id": u["update_id"],
            "message_id": msg.get("message_id"),
            "trace_id": trace_id,
        }
        assert ctx.kafka is not None
        await ctx.kafka.send_envelope_inner(ctx.settings.kafka_topic_alerts, payload)
        logger.info("[%s] telegram_callback_in -> kafka topic=%s", trace_id, ctx.settings.kafka_topic_alerts)
    except Exception:
        logger.exception("telegram_fallback_callback")
        if ctx.telegram and cq_id:
            try:
                await ctx.telegram.answer_callback_query(cq_id, text="Lỗi", show_alert=True)
            except Exception:
                pass
    return True


async def _lock_heartbeat(redis_cli: redis.Redis, lock_key: str, stop_event: asyncio.Event) -> None:
    """Gia hạn TTL của khóa mỗi 5s để chứng minh Worker vẫn đang chạy thực sự."""
    try:
        while not stop_event.is_set():
            # Use wait instead of raw sleep so we can interrupt it instantly
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5.0)
                if stop_event.is_set():
                    break
            except asyncio.TimeoutError:
                await redis_cli.expire(lock_key, 15)
    except asyncio.CancelledError:
        pass


async def _process_stream_entry(
    ctx: WorkerHandlerContext,
    msg_id: str,
    fields: dict[str, str],
) -> None:
    raw = fields.get("data") or fields.get("payload") or "{}"
    lock_key = f"omni:lock:{msg_id}"

    # Banking-grade Idempotency: Lấy stable trace_id trực tiếp từ Stream Field
    _stable_id = fields.get("_stable_id")
    if not _stable_id:
        try:
            _pre = json.loads(raw)
            _stable_id = str(_pre.get("trace_id") or msg_id)
        except Exception:
            _stable_id = msg_id
    retry_key = f"omni:retry:{_stable_id}"

    # Banking-grade Idempotency: Chỉ xử lý nếu lấy được Khóa cứng (TTL 15s)
    acquired = await ctx.redis.set(lock_key, "locked", nx=True, ex=15)
    if not acquired:
        logger.info("[Kafka Guard] Lock %s is active. Skipping message %s.", lock_key, msg_id)
        return

    hb_stop = asyncio.Event()
    hb_task = asyncio.create_task(_lock_heartbeat(ctx.redis, lock_key, hb_stop))
    t0 = time.perf_counter()
    trace = f"stream-{msg_id}"
    stream_started = False
    
    try:
        payload: dict[str, Any] = json.loads(raw)
        payload.setdefault("trace_id", trace)
        trace = str(payload.get("trace_id"))
        log_start_request(
            trace,
            phase="stream_consumer",
            redis_msg_id=msg_id,
            source=payload.get("source"),
            chat_id=payload.get("chat_id"),
        )
        stream_started = True
        logger.info("[%s] stream_read redis_msg_id=%s", trace, msg_id)
        # Master Plan V3: omni-alerts → prober only (diagnostic pipeline → evidence topic); reasoning in kafka_evidence_loop.
        if payload.get("chat_id") is not None:
            try:
                await ctx.redis.setex(
                    f"omni:evidence_reply:{trace}",
                    900,
                    json.dumps({"chat_id": int(payload["chat_id"])}),
                )
            except Exception:
                logger.warning("[%s] evidence_reply context not stored", trace)
        ev = build_anomaly_event_from_alert_payload(payload)
        await run_diagnostic_pipeline(ctx, ev)
        dur_ms = (time.perf_counter() - t0) * 1000.0
        log_end_request(
            trace,
            phase="stream_consumer",
            status="ok",
            duration_ms=dur_ms,
            out_len=0,
        )

        await ctx.redis.delete(retry_key)

    except Exception as e:
        dur_ms = (time.perf_counter() - t0) * 1000.0
        if stream_started:
            log_end_request(
                trace, phase="stream_consumer", status="error", duration_ms=dur_ms, error=f"{type(e).__name__}: {e}"
            )
        else:
            logger.exception("[%s] event=stream_parse_error redis_msg_id=%s", trace, msg_id)
            
        await ctx.ledger.record_exception(e, phase="4", component="handler", swallow_errors=True)
        
        # Logic Retry Sinh Tử (Exponential Backoff qua ZSET)
        retry_count = await ctx.redis.incr(retry_key)
        
        try:
            pl = json.loads(raw)
            dlq_trace = str(pl.get("trace_id") or trace)
        except Exception:
            dlq_trace = trace
            pl = {"raw": raw}
            
        if retry_count < 3:
            # Lần 1: 5s, Lần 2: 15s
            delay_sec = 5 if retry_count == 1 else 15
            retry_at = time.time() + delay_sec
            logger.warning("[%s] Error. Retrying %d/3 at +%ds (ZADD delayed_queue).", trace, retry_count, delay_sec)
            # Giữ nguyên raw data, attach msg_id và _stable_id độc lập
            zset_payload = json.dumps({
                "msg_id": msg_id, 
                "data": raw, 
                "_stable_id": _stable_id
            })
            await ctx.redis.zadd("omni:delayed_queue", {zset_payload: retry_at})
        else:
            logger.error("[%s] Fatal Retry >=3. Sending to DLQ.", trace)
            error_ctx = {
                "error_type": type(e).__name__,
                "component": "LLM_or_Tools" if "ollama" in str(e).lower() else "omni_worker",
                "message": str(e),
                "trace_id": dlq_trace
            }
            dlq_payload = {"error_context": json.dumps(error_ctx), "trace_id": dlq_trace, "data": raw}
            assert ctx.kafka is not None
            await ctx.kafka.send_dict(ctx.settings.kafka_topic_dlq, dlq_payload)
            await ctx.redis.delete(retry_key)

    finally:
        hb_stop.set()
        hb_task.cancel()
        await ctx.redis.delete(lock_key)


async def delayed_queue_loop(ctx: WorkerHandlerContext, stop: asyncio.Event) -> None:
    """The Chrono Loop - Đưa tin nhắn tới hạn quay lại Stream chính."""
    await ctx.scout_ready.wait()
    while not stop.is_set():
        try:
            now = time.time()
            items = await ctx.redis.zrangebyscore("omni:delayed_queue", min=0, max=now, start=0, num=10)
            for item in items:
                # Dùng ZREM để tránh Race-condition nếu chạy nhiều Pods
                removed = await ctx.redis.zrem("omni:delayed_queue", item)
                if removed == 1:
                    wrapped = json.loads(item)
                    assert ctx.kafka is not None
                    await ctx.kafka.send_dict(
                        ctx.settings.kafka_topic_alerts,
                        {
                            "data": wrapped["data"],
                            "_stable_id": wrapped.get("_stable_id", wrapped["msg_id"]),
                        },
                    )
        except Exception as e:
            logger.error("delayed_queue_loop error: %s", e)
        # Nếu Circuit Breaker đang hoạt động, chạy nhanh hơn (0.2s) để giải phóng rác
        try:
            cb_active = await ctx.redis.get("omni:circuit_breaker:active")
            sleep_sec = 0.2 if cb_active == b"1" or cb_active == "1" else 1.0
        except Exception:
            sleep_sec = 1.0
        await asyncio.sleep(sleep_sec)


async def circuit_breaker_loop(ctx: WorkerHandlerContext, stop: asyncio.Event) -> None:
    """Dedicated loop for Circuit Breaker monitoring to prevent OOM / resource exhaustion."""
    from workers.metrics_exporter import set_circuit_breaker_active
    cb_limit = getattr(ctx.settings, "cb_max_delayed_queue", 5000)
    await ctx.scout_ready.wait()
    while not stop.is_set():
        try:
            zset_size = await ctx.redis.zcard("omni:delayed_queue")
            if zset_size > cb_limit:
                logger.error("[CIRCUIT_BREAKER] ZSET size=%d > limit=%d. Tripping breaker!", zset_size, cb_limit)
                set_circuit_breaker_active(1)
                await ctx.redis.setex("omni:circuit_breaker:active", 60, "1")
            else:
                cb_now = await ctx.redis.get("omni:circuit_breaker:active")
                if cb_now in (b"1", "1"):
                    set_circuit_breaker_active(0)
                    await ctx.redis.delete("omni:circuit_breaker:active")
                    logger.info("[CIRCUIT_BREAKER] Cleared. ZSET=%d", zset_size)
        except Exception as e:
            logger.warning("circuit_breaker_loop error: %s", e)
        await asyncio.sleep(2.0)


async def kafka_alerts_loop(ctx: WorkerHandlerContext, stop: asyncio.Event) -> None:
    from aiokafka import AIOKafkaConsumer

    ws = ctx.settings
    await ctx.scout_ready.wait()
    consumer = AIOKafkaConsumer(
        ws.kafka_topic_alerts,
        bootstrap_servers=ws.kafka_bootstrap_servers,
        group_id=ws.consumer_group,
        enable_auto_commit=False,
        client_id=ws.consumer_name,
    )
    await consumer.start()
    try:
        async for msg in consumer:
            if stop.is_set():
                break
            try:
                fields = decode_kafka_value_to_fields(msg.value)
                mid = kafka_msg_id(msg.topic, msg.partition, msg.offset)
                await _process_stream_entry(ctx, mid, fields)
                await consumer.commit()
            except Exception as e:
                await ctx.ledger.record_exception(e, phase="4", component="kafka_alerts_loop", swallow_errors=True)
                logger.exception("kafka_alerts_loop message error: %s", e)
                await asyncio.sleep(0.5)
    finally:
        await consumer.stop()


async def kafka_evidence_loop(ctx: WorkerHandlerContext, stop: asyncio.Event) -> None:
    """Analyst path: consume ``omni-diagnostic-evidence`` only (Master Plan V3)."""
    from aiokafka import AIOKafkaConsumer

    ws = ctx.settings
    await ctx.scout_ready.wait()
    consumer = AIOKafkaConsumer(
        ws.kafka_topic_diagnostic_evidence,
        bootstrap_servers=ws.kafka_bootstrap_servers,
        group_id=ws.consumer_group_analyst,
        enable_auto_commit=False,
        client_id=ws.consumer_name_analyst,
    )
    await consumer.start()
    try:
        async for msg in consumer:
            if stop.is_set():
                break
            try:
                fields = decode_kafka_value_to_fields(msg.value)
                await reason_from_diagnostic_evidence(ctx, fields)
                await consumer.commit()
            except Exception as e:
                await ctx.ledger.record_exception(e, phase="4", component="kafka_evidence_loop", swallow_errors=True)
                logger.exception("kafka_evidence_loop message error: %s", e)
                await asyncio.sleep(0.5)
    finally:
        await consumer.stop()


async def telegram_loop(ctx: WorkerHandlerContext, stop: asyncio.Event) -> None:
    if ctx.telegram is None:
        return
    await ctx.scout_ready.wait()
    offset: int | None = None
    while not stop.is_set():
        try:
            data = await ctx.telegram.get_updates(offset=offset, timeout=25)
        except Exception as e:
            await ctx.ledger.record_exception(e, phase="4", component="telegram_loop", swallow_errors=True)
            await asyncio.sleep(3)
            continue
        for u in data.get("result") or []:
            offset = int(u["update_id"]) + 1
            if await _handle_telegram_fallback_callback(ctx, u):
                continue
            s = summarize_message_update(u)
            if not s or not s.text:
                continue
            trace_id = f"tg-{s.chat_id}-{s.update_id}-{s.message_id}"
            payload = {
                "text": s.text,
                "chat_id": s.chat_id,
                "source": "telegram",
                "update_id": s.update_id,
                "message_id": s.message_id,
                "trace_id": trace_id,
            }
            assert ctx.kafka is not None
            logger.info("[%s] telegram_in -> kafka topic=%s", trace_id, ctx.settings.kafka_topic_alerts)
            await ctx.kafka.send_envelope_inner(ctx.settings.kafka_topic_alerts, payload)


async def build_context() -> WorkerHandlerContext:
    ws = WorkerSettings()
    r = await connect_redis(ws)
    ollama = OllamaClient(base_url=ws.ollama_base_url)
    pg_settings = PostgresRAGSettings()
    pg_pool = await init_pg_pool(pg_settings)
    vector_store = PGVectorStore(pg_pool)
    ledger = ErrorLedger(pg_pool, owns_pool=False)
    sem = RedisOllamaSemaphore(
        r,
        max_slots=ws.ollama_num_parallel,
        lease_ttl_sec=ws.ollama_lease_ttl_sec,
    )
    await sem.init_pool()
    await ledger.ensure_ready()

    tg: TelegramClient | None = None
    if ws.telegram_enabled:
        try:
            ts = TelegramBotSettings()
            if ts.bot_token:
                tg = TelegramClient.from_settings(ts)
        except Exception as e:
            logger.warning("telegram disabled: %s", e)

    producer = await create_producer(ws.kafka_bootstrap_servers)
    kafka_bus = KafkaBus(producer)

    return WorkerHandlerContext(
        settings=ws,
        redis=r,
        ollama=ollama,
        vector_store=vector_store,
        ledger=ledger,
        semaphore=sem,
        telegram=tg,
        telegram_chat_id=None,
        kafka=kafka_bus,
    )


async def _run_autonomous_safe(ctx: WorkerHandlerContext) -> None:
    try:
        await run_deep_scout_autonomous(ctx, periodic=False)
    except Exception as e:
        logger.exception("deep_scout_autonomous startup: %s", e)
        await ctx.ledger.record_exception(e, phase="4", component="deep_scout_autonomous", swallow_errors=True)


def _worker_background_tasks(ctx: WorkerHandlerContext, stop: asyncio.Event) -> list[asyncio.Task[Any]]:
    """Split Kafka and periodic loops by ``OMNI_WORKER_ROLE`` (Master Plan V3)."""
    role = ctx.settings.worker_role
    tasks: list[asyncio.Task[Any]] = []
    if role == "executor":
        tasks.append(asyncio.create_task(kafka_actions_loop(ctx, stop), name="kafka_actions_loop"))
        return tasks
    if role in ("full", "prober"):
        tasks.extend(
            [
                asyncio.create_task(kafka_alerts_loop(ctx, stop), name="kafka_alerts_loop"),
                asyncio.create_task(delayed_queue_loop(ctx, stop), name="delayed_queue_loop"),
                asyncio.create_task(circuit_breaker_loop(ctx, stop), name="circuit_breaker"),
            ]
        )
        if ctx.telegram is not None:
            tasks.append(asyncio.create_task(telegram_loop(ctx, stop), name="telegram_loop"))
    if role in ("full", "analyst"):
        tasks.append(asyncio.create_task(kafka_evidence_loop(ctx, stop), name="kafka_evidence_loop"))
    if role in ("full", "core"):
        tasks.extend(
            [
                asyncio.create_task(deep_scout_periodic_loop(ctx, stop), name="deep_scout_periodic"),
                asyncio.create_task(autonomous_forecast_loop(ctx, stop), name="autonomous_forecast"),
                asyncio.create_task(baseline_snapshot_loop(ctx, stop), name="baseline_snapshot"),
            ]
        )
        if ctx.settings.autonomous_decider_enabled:
            tasks.append(asyncio.create_task(autonomous_decider_loop(ctx, stop), name="autonomous_decider"))
        if ctx.settings.proactive_enabled:
            tasks.append(asyncio.create_task(proactive_evaluate_loop(ctx, stop), name="proactive_evaluate"))
            tasks.append(asyncio.create_task(kafka_proactive_incidents_loop(ctx, stop), name="kafka_proactive_incidents"))
    return tasks


async def run_worker() -> None:
    # V6.3: JSON Structured Logging (The Chief Architect's Order)
    from pythonjsonlogger import jsonlogger
    
    root_logger = logging.getLogger()
    log_handler = logging.StreamHandler()
    
    class CustomJsonFormatter(jsonlogger.JsonFormatter):
        def add_fields(self, log_record, record, message_dict):
            super(CustomJsonFormatter, self).add_fields(log_record, record, message_dict)
            if not log_record.get('timestamp'):
                log_record['timestamp'] = record.created
            if not log_record.get('level'):
                log_record['level'] = record.levelname
            if not log_record.get('logger'):
                log_record['logger'] = record.name

    formatter = CustomJsonFormatter('%(timestamp)s %(level)s %(logger)s %(message)s')
    log_handler.setFormatter(formatter)
    root_logger.handlers = [log_handler]
    root_logger.setLevel(logging.INFO)
    
    # httpx INFO logs full request URL — Telegram base_url embeds bot token; never log that.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    stop = asyncio.Event()
    ctx = await build_context()
    logger.info("omni_worker starting worker_role=%s", ctx.settings.worker_role)
    setup_otel_tracing(
        service_name=ctx.settings.otel_service_name,
        otlp_endpoint=ctx.settings.otel_exporter_otlp_endpoint,
        enabled=bool(ctx.settings.otel_tracing_enabled and ctx.settings.otel_exporter_otlp_endpoint),
    )
    start_prometheus_server(ctx.settings.metrics_listen_host, ctx.settings.metrics_listen_port)
    asyncio.create_task(
        observability_metrics_loop(
            redis=ctx.redis,
            kill_switch_key=ctx.settings.proactive_kill_switch_key,
            ollama_base_url=ctx.settings.ollama_base_url,
            stop=stop,
            stream_keys=(),
            interval_sec=15.0,
        ),
        name="observability_metrics",
    )

    summary = DeepScoutSummary()
    role = ctx.settings.worker_role
    if role in ("analyst", "executor"):
        ctx.scout_ready.set()
    else:
        try:
            summary = await run_deep_scout(ctx)
            set_last_scout_timestamp()
        except Exception as e:
            logger.exception("deep_scout startup failed: %s", e)
            await ctx.ledger.record_exception(e, phase="4", component="deep_scout", swallow_errors=True)
        ctx.scout_ready.set()
        if role in ("full", "core"):
            asyncio.create_task(_run_autonomous_safe(ctx), name="deep_scout_autonomous_startup")

    if (
        role not in ("analyst", "executor")
        and ctx.telegram is not None
        and ctx.settings.telegram_admin_chat_id is not None
    ):
        try:
            cid = int(ctx.settings.telegram_admin_chat_id)
            msg = (
                f"[DeepScout] Đã soi xong: Nodes≈{summary.n_nodes}, Pods={summary.n_pods}, "
                f"Services={summary.n_services}. Prometheus={ctx.settings.prometheus_url[:96]}"
            )
            if summary.errors:
                msg += f" (warnings={len(summary.errors)})"
            await ctx.telegram.send_message(cid, msg[:3900])
        except Exception as e:
            logger.warning("deep_scout telegram report: %s", e)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    tasks = _worker_background_tasks(ctx, stop)

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        stop.set()
        shutdown_otel_tracing()
        await ctx.ollama.aclose()
        if ctx.telegram:
            await ctx.telegram.aclose()
        if ctx.kafka:
            await ctx.kafka.close()
        await ctx.vector_store.close()
        await ctx.redis.aclose()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
