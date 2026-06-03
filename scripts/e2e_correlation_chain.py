#!/usr/bin/env python3
"""E2E: SIEM correlation-chain pipeline (Phase 5, live cluster).

Injects a 3-event lateral-movement chain (cross-host, shared source_ip) plus
50 unrelated noise events into the brain-go Kafka input topic, then consumes
``omni-siem-chains`` and asserts EXACTLY ONE chain is emitted (0 false positives).

Prerequisites:
  - brain-go running with BRAIN_TRANSPORT=kafka and CORR_GRAPH_ENABLED=true.
  - Kafka reachable (override via E2E_KAFKA_BOOTSTRAP).

Usage:
  E2E_KAFKA_BOOTSTRAP=localhost:9092 .venv/bin/python scripts/e2e_correlation_chain.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

BOOTSTRAP = os.getenv("E2E_KAFKA_BOOTSTRAP", "localhost:9092")
RAW_TOPIC = os.getenv("E2E_SIEM_RAW_TOPIC", "omni-siem-raw")
CHAINS_TOPIC = os.getenv("E2E_SIEM_CHAINS_TOPIC", "omni-siem-chains")
TENANT = os.getenv("E2E_TENANT", "e2e-acme")
WAIT_SECONDS = int(os.getenv("E2E_WAIT_SECONDS", "30"))
ATTACK_IP = "203.0.113.77"


def _raw(incident_id: str, severity: str, category: str, source_ip: str, desc: str) -> dict:
    return {
        "id": incident_id,
        "tenant_id": TENANT,
        "severity": severity,
        "source": "e2e-injector",
        "category": category,
        "timestamp_unix": int(time.time()),
        "source_ip": source_ip,
        "description": desc,
        "schema_version": "1.0.0",
    }


def _build_events() -> list[dict]:
    events: list[dict] = []
    # 50 noise events: distinct ip + user + host, benign single category.
    for i in range(50):
        events.append(
            _raw(
                str(uuid.uuid4()),
                "low",
                "network_anomaly",
                f"10.20.{i // 256}.{i % 256}",
                f"user=noise{i} host=nh{i}",
            )
        )
    # The attack: one ip pivoting across 3 hosts, recon→access→execution, shared user.
    events.append(_raw(str(uuid.uuid4()), "medium", "port_scan", ATTACK_IP, "port scan user=mallory host=web-01"))
    events.append(_raw(str(uuid.uuid4()), "high", "auth_failure", ATTACK_IP, "auth failure user=mallory host=db-02"))
    events.append(_raw(str(uuid.uuid4()), "high", "new_process", ATTACK_IP, "new process user=mallory host=app-03 process=nc"))
    return events


async def main() -> int:
    producer = AIOKafkaProducer(
        bootstrap_servers=BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode(),
    )
    consumer = AIOKafkaConsumer(
        CHAINS_TOPIC,
        bootstrap_servers=BOOTSTRAP,
        group_id=f"e2e-chain-{uuid.uuid4().hex[:8]}",
        auto_offset_reset="latest",
        enable_auto_commit=True,
    )
    await producer.start()
    await consumer.start()
    try:
        # Subscribe first (latest offset), then inject.
        await asyncio.sleep(2)
        for ev in _build_events():
            await producer.send_and_wait(RAW_TOPIC, value=ev)
        print(f"[e2e] injected 53 events (50 noise + 3 attack) to {RAW_TOPIC}")

        chains: list[dict] = []
        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(consumer.getone(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            try:
                chain = json.loads(msg.value)
            except (ValueError, TypeError):
                continue
            if chain.get("tenant_id") == TENANT:
                chains.append(chain)

        ours = [c for c in chains if c.get("tenant_id") == TENANT]
        print(f"[e2e] received {len(ours)} chains for tenant {TENANT}")
        if len(ours) != 1:
            print(f"[e2e] FAIL: expected exactly 1 chain (0 false positives), got {len(ours)}")
            for c in ours:
                print("  -", c.get("attack_category"), c.get("common_dimensions"))
            return 1
        chain = ours[0]
        dims = chain.get("common_dimensions") or []
        if not any(d.get("type") == "ip" and d.get("value") == ATTACK_IP for d in dims):
            print(f"[e2e] FAIL: chain missing attack ip dimension: {dims}")
            return 1
        print(
            f"[e2e] PASS: 1 chain, category={chain.get('attack_category')} "
            f"confidence={chain.get('confidence')} dimensions={dims}"
        )
        return 0
    finally:
        await consumer.stop()
        await producer.stop()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
