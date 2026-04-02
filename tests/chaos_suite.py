import asyncio
import os
import time
import json

import redis.asyncio as redis

STREAM_INBOUND = "events:inbound"
DLQ = "events:dlq"
DELAYED_QUEUE = "omni:delayed_queue"

async def get_redis_client():
    env_cluster = os.environ.get("OMNI_REDIS_CLUSTER", "false").lower() == "true"
    host = os.environ.get("OMNI_REDIS_HOST", "localhost")
    port = int(os.environ.get("OMNI_REDIS_PORT", "6379"))
    
    if env_cluster:
        from redis.asyncio.cluster import RedisCluster
        from redis.cluster import ClusterNode
        return RedisCluster(startup_nodes=[ClusterNode(host, port)], decode_responses=True)
    else:
        return redis.Redis(host=host, port=port, db=0, decode_responses=False)

async def main():
    r = await get_redis_client()
    print("--- PREPARING CHAOS ENVIRONMENT ---")
    await r.delete(STREAM_INBOUND)
    await r.delete(DLQ)
    await r.delete(DELAYED_QUEUE)

    # Recreate Consumer Group because deleting the stream destroyed the group
    try:
        await r.xgroup_create(STREAM_INBOUND, "omni", id="0", mkstream=True)
    except Exception as e:
        pass

    # 1. TẠO BÃO POISON STORM
    print("\n--- [KỊCH BẢN 1]: SPHINX POISON STORM (1000 MALFORMED MESSAGES) ---")
    print("Injecting 1000 corrupted payloads...")
    for i in range(1000):
        await r.xadd(STREAM_INBOUND, {"data": f"{{ \"broken\": {i}, MISSING_QUOTES }}"})

    print("Injection complete. Monitoring ZSET Backoff routing & DLQ...")

    start_monitor = time.time()
    last_dlq = -1
    last_zset = -1
    
    while time.time() - start_monitor < 40:
        dlq_len = await r.xlen(DLQ)
        pel_info = await r.xinfo_groups(STREAM_INBOUND)
        pel_len = pel_info[0]["pending"] if pel_info else 0
        zset_len = await r.zcard(DELAYED_QUEUE)
        
        if dlq_len != last_dlq or zset_len != last_zset:
            print(f"[{time.time()-start_monitor:.1f}s] PEL: {pel_len} | ZSET (Retrying): {zset_len} | DLQ (Dead): {dlq_len}/1000")
            last_dlq = dlq_len
            last_zset = zset_len
            
        if dlq_len >= 1000:
            print(">>> THÀNH CÔNG: The storm was completely routed to DLQ via Non-Blocking Retry! <<<")
            break
        await asyncio.sleep(1)

    # 4. KỊCH BẢN 4: THE TWIN ZOMBIES (IDEMPOTENCY GUARD)
    print("\n--- [KỊCH BẢN 4]: THE TWIN ZOMBIES (IDEMPOTENCY GUARD) ---")
    trace_id = "chaos-twin-123"
    tool_name = "k8s_rollout_restart"
    lock_key = f"omni:tool_executed:{tool_name}:{trace_id}"
    
    print(f"Bơm Fake Lock: SET {lock_key} = success")
    await r.setex(lock_key, 60, "success")
    
    print("Gửi tin nhắn chứa lệnh rollout restart...")
    msg_payload = {"text": f"rollout restart deployment test-app", "trace_id": trace_id}
    await r.xadd(STREAM_INBOUND, {"data": json.dumps(msg_payload)})
    
    print("Chờ Worker xử lý (nó sẽ bị Guard chặn)...")
    await asyncio.sleep(5)
    
    # 5. KỊCH BẢN 5: BÃO 5001 (CIRCUIT BREAKER & GATEWAY)
    print("\n--- [KỊCH BẢN 5]: BÃO 5001 (OOM PROTECTION & GATEWAY BACKPRESSURE) ---")
    cb_limit = 5000
    print(f"Xả {cb_limit + 1} tin nhắn rác vào Delayed Queue...")
    
    # Bơm ZSET direct để nhanh
    now = time.time()
    pipe = r.pipeline()
    for i in range(cb_limit + 1):
        z_payload = json.dumps({"msg_id": f"chaos-{i}", "data": "{}", "_stable_id": f"chaos-trace-{i}"})
        pipe.zadd(DELAYED_QUEUE, {z_payload: now + 100})
    await pipe.execute()
    
    print("Đang check Circuit Breaker Metric & Redis Global Flag...")
    await asyncio.sleep(2)
    
    zset_len = await r.zcard(DELAYED_QUEUE)
    cb_redis_flag = await r.get("omni:circuit_breaker:active")
    
    print(f"ZSET Length: {zset_len}")
    print(f"Redis CB Flag: {cb_redis_flag}")
    
    if cb_redis_flag in (b"1", "1"):
        print(">>> THÀNH CÔNG: Circuit Breaker đã ngắt mạch! Gateway sẽ trả về 503/429. <<<")
    else:
        print(">>> THẤT BẠI: Circuit Breaker không hoạt động! <<<")

    # Dọn dẹp rác chaos
    await r.delete(DELAYED_QUEUE)
    await r.delete("omni:circuit_breaker:active")
    
    print("\n--- ALL CHAOS SCENARIOS COMPLETED ---")
    await r.aclose()

if __name__ == "__main__":
    asyncio.run(main())
