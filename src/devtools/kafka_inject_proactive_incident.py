"""Inject one proactive incident onto Kafka (used by full_system_audit / lab)."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys


async def _run(inner: dict) -> None:
    from messaging.kafka_bus import KafkaBus, create_producer
    from workers.settings import WorkerSettings

    ws = WorkerSettings()
    p = await create_producer(ws.kafka_bootstrap_servers)
    try:
        bus = KafkaBus(p)
        await bus.send_dict(ws.kafka_topic_proactive_incidents, {"data": inner})
    finally:
        await p.stop()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("payload_b64", help="base64(JSON object) proactive incident body")
    args = ap.parse_args()
    inner = json.loads(base64.b64decode(args.payload_b64).decode("utf-8"))
    asyncio.run(_run(inner))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
