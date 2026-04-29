#!/usr/bin/env python3
"""
DLQ Replay Tool — Kafka topic ``omni-dlq`` (former Redis Stream events:dlq).

List / replay by decoding JSON message values. Kafka has no per-message delete; replay re-produces
to the alerts topic without removing DLQ entries (append-only log).

Usage:
  OMNI_KAFKA_BOOTSTRAP_SERVERS=kafka:9090 python scripts/dlq_replay.py --list
  python scripts/dlq_replay.py --replay-network --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any

KAFKA_BOOTSTRAP = os.getenv("OMNI_KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
DLQ_TOPIC = os.getenv("OMNI_KAFKA_TOPIC_DLQ", os.getenv("OMNI_STREAM_DLQ", "omni-dlq"))
ALERTS_TOPIC = os.getenv("OMNI_KAFKA_TOPIC_ALERTS", os.getenv("OMNI_STREAM_INBOUND", "omni-alerts"))

# SIEM Redis Stream DLQ (FinGuard Redis, separate from Kafka DLQ)
SIEM_REDIS_URL = os.getenv("SIEM_REDIS_URL") or os.getenv("OMNI_REDIS_FG_URL") or "redis://localhost:6379/0"
SIEM_DLQ_STREAM = os.getenv("SIEM_REDIS_DLQ", "stream:actionable_incidents:dlq")
SIEM_MAIN_STREAM = os.getenv("SIEM_REDIS_STREAM", "stream:actionable_incidents")

NETWORK_ERROR_TYPES = {
    "HTTPStatusError",
    "ConnectionError",
    "ConnectError",
    "TimeoutException",
    "ReadTimeout",
    "ConnectTimeout",
    "RemoteProtocolError",
    "NetworkError",
}

FATAL_ERROR_TYPES = {
    "JSONDecodeError",
    "ValueError",
    "KeyError",
    "AttributeError",
    "ValidationError",
}


def _decode_record(val: bytes) -> dict[str, Any]:
    try:
        d = json.loads(val.decode("utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


async def _consumer_from_earliest(topic: str, max_messages: int) -> list[tuple[str, dict[str, Any]]]:
    from aiokafka import AIOKafkaConsumer

    out: list[tuple[str, dict[str, Any]]] = []
    c = AIOKafkaConsumer(
        topic,
        bootstrap_servers=KAFKA_BOOTSTRAP.strip(),
        group_id=f"dlq-replay-tool-{int(time.time())}",
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    await c.start()
    try:
        while len(out) < max_messages:
            pack = await c.getmany(timeout_ms=3000, max_records=50)
            if not pack:
                break
            for _tp, batch in pack.items():
                for msg in batch:
                    out.append((f"kafka-{msg.partition}-{msg.offset}", _decode_record(msg.value)))
                    if len(out) >= max_messages:
                        return out
    finally:
        await c.stop()
    return out


async def list_dlq(error_type_filter: str | None = None) -> None:
    rows = await _consumer_from_earliest(DLQ_TOPIC, 500)
    if not rows:
        print(f"[EMPTY] DLQ topic '{DLQ_TOPIC}' has no readable messages (or timeout).")
        return
    print(f"\n{'─' * 80}\n  DLQ Review — {DLQ_TOPIC} (up to {len(rows)})\n{'─' * 80}")
    shown = 0
    for mid, fields in rows:
        try:
            error_ctx = json.loads(fields.get("error_context", "{}"))
        except Exception:
            error_ctx = {}
        etype = error_ctx.get("error_type", "UNKNOWN")
        trace_id = fields.get("trace_id") or error_ctx.get("trace_id", "?")
        if error_type_filter and error_type_filter.lower() not in str(etype).lower():
            continue
        shown += 1
        print(f"\n  [{shown}] offset_id={mid} trace_id={trace_id} error_type={etype}")
    if shown == 0:
        print(f"\n  No messages matching filter: error_type='{error_type_filter}'")


async def replay_messages(
    error_type_filter: str | None = None,
    network_only: bool = False,
    msg_id_filter: str | None = None,
    dry_run: bool = False,
) -> None:
    from aiokafka import AIOKafkaProducer

    rows = await _consumer_from_earliest(DLQ_TOPIC, 1000)
    if not rows:
        print("[INFO] DLQ is empty.")
        return
    p = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP.strip(), enable_idempotence=True, acks="all")
    await p.start()
    replayed = 0
    skipped_fatal = 0
    skipped_filter = 0
    try:
        for mid, fields in rows:
            if msg_id_filter and mid != msg_id_filter:
                continue
            try:
                error_ctx = json.loads(fields.get("error_context", "{}"))
            except Exception:
                error_ctx = {}
            etype = error_ctx.get("error_type", "UNKNOWN")
            if etype in FATAL_ERROR_TYPES:
                skipped_fatal += 1
                continue
            if error_type_filter and error_type_filter.lower() not in str(etype).lower():
                skipped_filter += 1
                continue
            if network_only and etype not in NETWORK_ERROR_TYPES:
                skipped_filter += 1
                continue
            raw_data = fields.get("data", "{}")
            trace_id = fields.get("trace_id") or error_ctx.get("trace_id", f"dlq-replay-{int(time.time())}")
            if dry_run:
                print(f"  [DRY-RUN] Would replay {mid} error_type={etype} trace_id={trace_id}")
                replayed += 1
                continue
            env = json.dumps({"data": raw_data, "_stable_id": trace_id}, ensure_ascii=False).encode("utf-8")
            await p.send_and_wait(ALERTS_TOPIC, value=env)
            print(f"  [REPLAYED] {mid} → {ALERTS_TOPIC}")
            replayed += 1
    finally:
        await p.stop()
    print(f"\n  Summary: replayed={replayed} skipped_fatal={skipped_fatal} skipped_filter={skipped_filter}")


async def siem_dlq_list(since: str | None = None) -> None:
    """List all entries in the SIEM Redis Stream DLQ."""
    import redis.asyncio as aioredis

    r = aioredis.from_url(SIEM_REDIS_URL, decode_responses=True)
    try:
        min_id = "-"
        if since:
            try:
                import datetime as _dt
                ts = _dt.datetime.fromisoformat(since.replace("Z", "+00:00"))
                min_id = str(int(ts.timestamp() * 1000))
            except Exception:
                print(f"[WARN] Could not parse --since={since!r}, using beginning of stream.")

        entries = await r.xrange(SIEM_DLQ_STREAM, min=min_id, max="+")
        if not entries:
            print(f"[EMPTY] SIEM DLQ stream '{SIEM_DLQ_STREAM}' has no messages.")
            return

        print(f"\n{'─' * 80}\n  SIEM DLQ — {SIEM_DLQ_STREAM} ({len(entries)} entries)\n{'─' * 80}")
        for i, (msg_id, fields) in enumerate(entries, 1):
            ts_ms = int(msg_id.split("-")[0])
            ts_fmt = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_ms / 1000))
            print(
                f"\n  [{i}] id={msg_id} ts={ts_fmt}"
                f"\n       error={fields.get('error', '')[:120]}"
                f"\n       payload_preview={fields.get('payload', '')[:180]}"
            )
        print(f"\n  Total: {len(entries)} entries")
    finally:
        await r.aclose()


async def siem_dlq_replay(dry_run: bool = False, since: str | None = None) -> None:
    """Replay SIEM DLQ entries back into the main stream."""
    import redis.asyncio as aioredis

    r = aioredis.from_url(SIEM_REDIS_URL, decode_responses=True)
    try:
        min_id = "-"
        if since:
            try:
                import datetime as _dt
                ts = _dt.datetime.fromisoformat(since.replace("Z", "+00:00"))
                min_id = str(int(ts.timestamp() * 1000))
            except Exception:
                print(f"[WARN] Could not parse --since={since!r}, using beginning of stream.")

        entries = await r.xrange(SIEM_DLQ_STREAM, min=min_id, max="+")
        if not entries:
            print(f"[INFO] SIEM DLQ stream '{SIEM_DLQ_STREAM}' is empty.")
            return

        replayed = 0
        for msg_id, fields in entries:
            payload = fields.get("payload") or "{}"
            if dry_run:
                print(f"  [DRY-RUN] Would replay {msg_id} → {SIEM_MAIN_STREAM}")
                replayed += 1
                continue
            await r.xadd(SIEM_MAIN_STREAM, {"data": payload, "_dlq_replay_from": msg_id})
            print(f"  [REPLAYED] {msg_id} → {SIEM_MAIN_STREAM}")
            replayed += 1

        print(f"\n  Summary: replayed={replayed} dry_run={dry_run}")
    finally:
        await r.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="DLQ replay — Kafka + SIEM Redis Stream")
    parser.add_argument("--list", action="store_true", help="List Kafka DLQ messages")
    parser.add_argument("--replay", action="store_true", help="Replay Kafka DLQ messages")
    parser.add_argument("--replay-network", action="store_true", help="Replay network-error Kafka DLQ messages")
    parser.add_argument("--replay-id", metavar="MSG_ID", help="Replay a specific Kafka DLQ message by ID")
    parser.add_argument("--error-type", metavar="TYPE", help="Filter by error type")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    parser.add_argument("--siem-dlq-list", action="store_true", help="List SIEM Redis Stream DLQ entries")
    parser.add_argument("--siem-dlq-replay", action="store_true", help="Replay SIEM Redis DLQ → main stream")
    parser.add_argument("--siem-dlq-replay-since", metavar="ISO_TIMESTAMP", help="Only replay entries since ISO timestamp")
    args = parser.parse_args()

    if args.siem_dlq_list:
        asyncio.run(siem_dlq_list(since=args.siem_dlq_replay_since))
    elif args.siem_dlq_replay:
        asyncio.run(siem_dlq_replay(dry_run=args.dry_run, since=args.siem_dlq_replay_since))
    elif args.list:
        asyncio.run(list_dlq(error_type_filter=args.error_type))
    elif args.replay or args.replay_network or args.replay_id:
        asyncio.run(
            replay_messages(
                error_type_filter=args.error_type,
                network_only=args.replay_network,
                msg_id_filter=args.replay_id,
                dry_run=args.dry_run,
            )
        )
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
