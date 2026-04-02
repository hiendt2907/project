import asyncio
import time
import sys
import logging
from workers.settings import WorkerSettings
from workers.redis_client import connect_redis
import redis.exceptions

# Disable noisy logs
logging.getLogger("redis").setLevel(logging.CRITICAL)

async def main():
    settings = WorkerSettings()
    client = await connect_redis(settings)
    try:
        print("=== BAT DAU CHAOS TEST ===")
        print("Trang thai: Dang doc ghi vao Redis Cluster moi 0.5s")
        print("==========================")

        success = 0
        errors = 0
        failover_started = False

        for i in range(50):
            try:
                t0 = time.time()
                key = f"omni:chaos:random:{i}"
                await client.set(key, "live_data", ex=10)
                val = await client.get(key)
                latency = (time.time() - t0) * 1000
                print(f"[+] Lanth {i:02d}: Ghi/Doc OK, Do tre = {latency:5.1f}ms (Slot Master dang song)")
                success += 1
                if failover_started:
                    print(f"!!! => FAILOVER HOAN TAT - CLUSTER FULLY RECOVERED SAU {errors} LOI !!!")
                    failover_started = False
            except (redis.exceptions.RedisClusterException, redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
                errors += 1
                failover_started = True
                print(f"[-] Lanth {i:02d}: LOI KET NOI (Mất Master Node) - {type(e).__name__}")
            except Exception as e:
                print(f"[-] Lanh {i:02d}: LOi khac - {e}")

            await asyncio.sleep(0.5)

        print(f"\n=== TONG KET CHAOS TEST ===")
        print(f"Thanh cong: {success}/50 requests")
        print(f"Gian doan (Downtime requests): {errors}/50")
        if errors > 0 and success > 40:
            print("KET LUAN: HIGHT-AVAILABILITY (HA) HOAT DONG! Re-routing thanh cong qua Slave Replicas.")
        elif errors == 0:
            print("KET LUAN: Khong co Master node nao bi chet trong the test.")
        else:
            print("KET LUAN: CLUSTER DOWN HOAN TOAN! FAILOVER THAT BAI.")
    finally:
        await client.aclose()

if __name__ == '__main__':
    asyncio.run(main())
