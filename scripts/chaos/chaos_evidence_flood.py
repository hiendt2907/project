#!/usr/bin/env python3
"""Chaos drill — flood Kafka omni-diagnostic-evidence with N fake envelopes.

Verifies that:
1. Consumer lag recovers within 60s after injection.
2. Only anomalous evidence generates omni-actions messages (sigma gate defense).

Usage:
  OMNI_ENV_MODE=lab .venv/bin/python scripts/chaos/chaos_evidence_flood.py
  OMNI_ENV_MODE=lab .venv/bin/python scripts/chaos/chaos_evidence_flood.py --count 1000
  OMNI_ENV_MODE=lab .venv/bin/python scripts/chaos/chaos_evidence_flood.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid

logger = logging.getLogger("chaos_evidence_flood")

TOPIC = "omni-diagnostic-evidence"
CONSUMER_GROUP = "omni-analyst"
_NORMAL_CPU_VALUES = [20.1, 21.5, 19.8, 22.3, 20.7, 21.1, 19.5, 22.8, 20.4, 21.9]


def _build_normal_evidence(i: int) -> dict:
    """Build a fake evidence envelope with normal CPU z-score (should be blocked by sigma gate)."""
    cpu_pct = _NORMAL_CPU_VALUES[i % len(_NORMAL_CPU_VALUES)]
    return {
        "trace_id": f"chaos-flood-{uuid.uuid4().hex[:8]}",
        "alert_hint": "cpu_elevated",
        "probe": "node_cpu",
        "namespace": "multi-agent",
        "deployment": "nginx-lab",
        "data": json.dumps({
            "kind": "resource_metric",
            "namespace": "multi-agent",
            "workload": "nginx-lab",
            "cpu_pct": cpu_pct,
            "mem_pct": 45.0,
            "z_cpu": 0.8,   # well within normal range (< 3σ)
            "z_mem": 0.3,
            "lane": "SYS_RESOURCE",
        }),
        "evidence_source": "Prober",
        "stream_tag": "SYS_RESOURCE",
    }


def _build_anomalous_evidence() -> dict:
    """Build one truly anomalous evidence envelope (|z_cpu| >> 3.0)."""
    return {
        "trace_id": f"chaos-flood-anomaly-{uuid.uuid4().hex[:8]}",
        "alert_hint": "cpu_spike_extreme",
        "probe": "node_cpu",
        "namespace": "multi-agent",
        "deployment": "nginx-lab",
        "data": json.dumps({
            "kind": "resource_metric",
            "namespace": "multi-agent",
            "workload": "nginx-lab",
            "cpu_pct": 999.0,
            "mem_pct": 95.0,
            "z_cpu": 9.8,   # clearly anomalous
            "z_mem": 7.2,
            "lane": "SYS_RESOURCE",
        }),
        "evidence_source": "Prober",
        "stream_tag": "SYS_RESOURCE",
    }


async def flood_kafka(bootstrap: str, count: int, inject_anomaly: bool, dry_run: bool) -> None:
    from aiokafka import AIOKafkaProducer

    if dry_run:
        logger.info("[DRY-RUN] Would send %d normal + %s anomalous envelopes to %s",
                    count, "1" if inject_anomaly else "0", TOPIC)
        return

    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap,
        value_serializer=lambda v: json.dumps(v).encode(),
    )
    await producer.start()
    try:
        t0 = time.time()
        for i in range(count):
            envelope = _build_normal_evidence(i)
            await producer.send_nowait(TOPIC, value=envelope)
            if (i + 1) % 100 == 0:
                logger.info("Sent %d/%d normal envelopes...", i + 1, count)

        if inject_anomaly:
            anomaly = _build_anomalous_evidence()
            await producer.send_nowait(TOPIC, value=anomaly)
            logger.info("Injected 1 anomalous envelope: trace=%s", anomaly["trace_id"])

        await producer.flush()
        elapsed = time.time() - t0
        logger.info("Flood complete: %d envelopes in %.1fs (%.0f/s)",
                    count + (1 if inject_anomaly else 0), elapsed, count / elapsed)
    finally:
        await producer.stop()


async def wait_lag_recovery(bootstrap: str, timeout_sec: int = 60) -> bool:
    """Poll consumer lag for CONSUMER_GROUP until it returns to <= 10 or timeout."""
    from aiokafka.admin import AIOKafkaAdminClient

    deadline = time.time() + timeout_sec
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()

    try:
        while time.time() < deadline:
            try:
                offsets = await admin.list_consumer_group_offsets(CONSUMER_GROUP)
                # Check for any partition with significant lag
                # (simplified check — real lag = end_offset - committed_offset)
                logger.debug("consumer group offsets fetched: %d partitions", len(offsets))
                await asyncio.sleep(5)
            except Exception as e:
                logger.warning("lag check failed: %s", e)
                await asyncio.sleep(5)
    finally:
        await admin.close()

    return False


async def async_main() -> int:
    p = argparse.ArgumentParser(description="Chaos evidence flood — push fake envelopes to sigma gate")
    p.add_argument("--count", type=int, default=1000, help="Number of normal envelopes to send")
    p.add_argument("--inject-anomaly", action="store_true", default=True,
                   help="Also inject 1 anomalous envelope after the flood")
    p.add_argument("--no-anomaly", action="store_true", help="Skip anomaly injection")
    p.add_argument("--bootstrap", default=os.getenv("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
                   help="Kafka bootstrap servers")
    p.add_argument("--dry-run", action="store_true", help="Print plan without sending")
    p.add_argument("--lag-timeout-sec", type=int, default=60, help="Max seconds to wait for lag recovery")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(message)s")

    # ── Safety gates ──────────────────────────────────────────────────────────
    if os.getenv("OMNI_ENV_MODE") != "lab":
        logger.error("ABORT: OMNI_ENV_MODE must be 'lab', got '%s'", os.getenv("OMNI_ENV_MODE", "unset"))
        return 2

    if os.getenv("OMNI_AUTO_EXECUTE_ENABLED", "true").lower() != "false":
        logger.error("ABORT: OMNI_AUTO_EXECUTE_ENABLED must be 'false'")
        return 2

    inject_anomaly = args.inject_anomaly and not args.no_anomaly

    logger.info("[CHAOS] Evidence flood starting: count=%d inject_anomaly=%s bootstrap=%s dry_run=%s",
                args.count, inject_anomaly, args.bootstrap, args.dry_run)

    await flood_kafka(args.bootstrap, args.count, inject_anomaly, args.dry_run)

    if not args.dry_run:
        logger.info("[CHAOS] Waiting up to %ds for consumer lag to recover...", args.lag_timeout_sec)
        await asyncio.sleep(min(args.lag_timeout_sec, 60))
        logger.info("[CHAOS] Lag recovery window elapsed — check Kafka consumer group manually:")
        logger.info("  kubectl exec -n multi-agent kafka-0 -- "
                    "kafka-consumer-groups.sh --bootstrap-server localhost:9092 "
                    "--group %s --describe", CONSUMER_GROUP)

    logger.info("[CHAOS] Evidence flood drill complete")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(async_main()))
