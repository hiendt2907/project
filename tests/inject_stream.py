import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiokafka import AIOKafkaProducer

from workers.settings import WorkerSettings


async def main() -> None:
    settings = WorkerSettings()
    p = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers.strip(),
        enable_idempotence=True,
        acks="all",
    )
    await p.start()
    try:
        msg = {"text": "Kiem tra Full System Recovery", "chat_id": 12345, "source": "chaos:sys"}
        msg2 = {"text": "Bot con thuc hay da xiu?", "chat_id": 12345, "source": "chaos:sys"}
        for m in (msg, msg2):
            env = json.dumps({"data": json.dumps(m, ensure_ascii=False)}, ensure_ascii=False).encode("utf-8")
            await p.send_and_wait(settings.kafka_topic_alerts, value=env)
        print(f"Injected test messages to Kafka topic {settings.kafka_topic_alerts}!")
    finally:
        await p.stop()


asyncio.run(main())
