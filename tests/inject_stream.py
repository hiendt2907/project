import asyncio
import sys
import json
from workers.settings import WorkerSettings
from workers.redis_client import connect_redis

async def main():
    settings = WorkerSettings()
    client = await connect_redis(settings)
    try:
        msg = {"text": "Kiem tra Full System Recovery", "chat_id": 12345, "source": "chaos:sys"}
        msg2 = {"text": "Bot con thuc hay da xiu?", "chat_id": 12345, "source": "chaos:sys"}
        await client.xadd("events:inbound", {"data": json.dumps(msg)})
        await client.xadd("events:inbound", {"data": json.dumps(msg2)})
        print("Injected test messages to Redis Stream!")
    finally:
        await client.aclose()

asyncio.run(main())
