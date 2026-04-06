"""Chaos lab: Kafka omni-alerts poison messages + delayed queue / circuit breaker (Redis)."""

import asyncio
import json
import os
import time

import redis.asyncio as redis
from aiokafka import AIOKafkaProducer

KAFKA_BOOTSTRAP = os.environ.get("OMNI_KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
KAFKA_TOPIC_ALERTS = os.environ.get("OMNI_KAFKA_TOPIC_ALERTS", "omni-alerts")
DLQ_TOPIC = os.environ.get("OMNI_KAFKA_TOPIC_DLQ", "omni-dlq")
DELAYED_QUEUE = "omni:delayed_queue"


async def get_redis_client():
    url = os.environ.get("OMNI_REDIS_URL", "").strip()
    if url:
        client = redis.Redis.from_url(url, decode_responses=False)
    else:
        host = os.environ.get("OMNI_REDIS_HOST", "localhost")
        port = int(os.environ.get("OMNI_REDIS_PORT", "6379"))
        client = redis.Redis(host=host, port=port, db=0, decode_responses=False)
    await client.initialize()
    return client


async def main() -> None:
    r = await get_redis_client()
    producer = AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP.strip(),
        enable_idempotence=True,
        acks="all",
    )
    await producer.start()
    print("--- PREPARING CHAOS ENVIRONMENT (Kafka + Redis) ---")
    await r.delete(DELAYED_QUEUE)

    print("\n--- [KỊCH BẢN 1]: POISON MESSAGES → Kafka omni-alerts (worker → DLQ topic on fatal) ---")
    print("Injecting 200 corrupted payloads...")
    for i in range(200):
        bad = f'{{ "broken": {i}, MISSING_QUOTES }}'
        env = json.dumps({"data": bad}, ensure_ascii=False).encode("utf-8")
        await producer.send_and_wait(KAFKA_TOPIC_ALERTS, value=env)
    print("Injection complete. Check omni-dlq topic / worker logs for routing.")

    print("\n--- [KỊCH BẢN 4]: TWIN ZOMBIES (idempotency) ---")
    trace_id = "chaos-twin-123"
    tool_name = "k8s_rollout_restart"
    lock_key = f"omni:tool_executed:{tool_name}:{trace_id}"
    await r.setex(lock_key, 60, "success")
    msg_payload = {"text": "rollout restart deployment test-app", "trace_id": trace_id}
    env = json.dumps({"data": json.dumps(msg_payload)}, ensure_ascii=False).encode("utf-8")
    await producer.send_and_wait(KAFKA_TOPIC_ALERTS, value=env)
    await asyncio.sleep(5)

    print("\n--- [KỊCH BẢN 5]: CIRCUIT BREAKER (delayed ZSET) ---")
    cb_limit = 5000
    now = time.time()
    pipe = r.pipeline()
    for i in range(cb_limit + 1):
        z_payload = json.dumps({"msg_id": f"chaos-{i}", "data": "{}", "_stable_id": f"chaos-trace-{i}"})
        pipe.zadd(DELAYED_QUEUE, {z_payload: now + 100})
    await pipe.execute()
    await asyncio.sleep(2)
    zset_len = await r.zcard(DELAYED_QUEUE)
    cb_redis_flag = await r.get("omni:circuit_breaker:active")
    print(f"ZSET Length: {zset_len}")
    print(f"Redis CB Flag: {cb_redis_flag}")

    await r.delete(DELAYED_QUEUE)
    await r.delete("omni:circuit_breaker:active")
    await producer.stop()
    await r.aclose()
    print("\n--- ALL CHAOS SCENARIOS COMPLETED ---")


if __name__ == "__main__":
    asyncio.run(main())
