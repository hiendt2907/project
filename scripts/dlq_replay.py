#!/usr/bin/env python3
"""
DLQ Replay Tool — Phòng Hồi Sức Cấp Cứu cho events:dlq.

Usage:
  # Xem toàn bộ DLQ (review mode, không replay)
  python scripts/dlq_replay.py --list

  # Xem và lọc theo error_type
  python scripts/dlq_replay.py --list --error-type HTTPStatusError

  # Replay TẤT CẢ tin nhắn lỗi mạng (HTTPStatusError, ConnectionError, TimeoutError)
  python scripts/dlq_replay.py --replay-network

  # Replay tin nhắn cụ thể theo message ID
  python scripts/dlq_replay.py --replay-id 1774853037116-0

  # Replay theo error_type tùy chỉnh
  python scripts/dlq_replay.py --replay --error-type ConnectionError

  # Dry-run (không xóa khỏi DLQ, chỉ in ra sẽ replay gì)
  python scripts/dlq_replay.py --replay-network --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

# Network errors that are safe to replay (infrastructure errors, not logic errors)
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

# Fatal logic errors — NEVER replay these
FATAL_ERROR_TYPES = {
    "JSONDecodeError",
    "ValueError",
    "KeyError",
    "AttributeError",
    "ValidationError",
}

REDIS_URL = os.getenv("OMNI_REDIS_URL", "redis://redis:6379/0")
DLQ_STREAM = os.getenv("OMNI_STREAM_DLQ", "events:dlq")
INBOUND_STREAM = os.getenv("OMNI_STREAM_INBOUND", "events:inbound")


async def list_dlq(error_type_filter: str | None = None) -> None:
    """Liệt kê tin nhắn trong DLQ, tùy chọn lọc theo error_type."""
    import redis.asyncio as aioredis
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        messages = await r.xrange(DLQ_STREAM, count=500)
        if not messages:
            print(f"[EMPTY] DLQ stream '{DLQ_STREAM}' is empty. Nothing to review.")
            return

        print(f"\n{'─'*80}")
        print(f"  DLQ Review — {DLQ_STREAM} ({len(messages)} entries)")
        print(f"{'─'*80}")

        shown = 0
        for msg_id, fields in messages:
            try:
                error_ctx = json.loads(fields.get("error_context", "{}"))
            except Exception:
                error_ctx = {}

            etype = error_ctx.get("error_type", "UNKNOWN")
            component = error_ctx.get("component", "?")
            trace_id = fields.get("trace_id") or error_ctx.get("trace_id", "?")
            msg_preview = str(error_ctx.get("message", ""))[:80]
            is_network = etype in NETWORK_ERROR_TYPES
            is_fatal = etype in FATAL_ERROR_TYPES
            replayable = "✅ REPLAYABLE" if is_network else ("🚫 FATAL/LOGIC" if is_fatal else "⚠️  UNKNOWN")

            if error_type_filter and error_type_filter.lower() not in etype.lower():
                continue

            shown += 1
            print(f"\n  [{shown}] ID: {msg_id}")
            print(f"       trace_id  : {trace_id}")
            print(f"       error_type: {etype}  ({replayable})")
            print(f"       component : {component}")
            print(f"       message   : {msg_preview}")

        if shown == 0:
            print(f"\n  No messages matching filter: error_type='{error_type_filter}'")
        print(f"\n{'─'*80}\n")
    finally:
        await r.aclose()


async def replay_messages(
    error_type_filter: str | None = None,
    network_only: bool = False,
    msg_id_filter: str | None = None,
    dry_run: bool = False,
) -> None:
    """Replay tin nhắn từ DLQ về Inbound stream."""
    import redis.asyncio as aioredis
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        messages = await r.xrange(DLQ_STREAM, count=1000)
        if not messages:
            print(f"[INFO] DLQ is empty.")
            return

        replayed = 0
        skipped_fatal = 0
        skipped_filter = 0

        for msg_id, fields in messages:
            # Lọc theo msg_id cụ thể nếu có
            if msg_id_filter and msg_id != msg_id_filter:
                continue

            try:
                error_ctx = json.loads(fields.get("error_context", "{}"))
            except Exception:
                error_ctx = {}

            etype = error_ctx.get("error_type", "UNKNOWN")

            # CHẶN: Không bao giờ replay lỗi logic/JSON
            if etype in FATAL_ERROR_TYPES:
                print(f"  [SKIP-FATAL] {msg_id} error_type={etype} — logical error, not replayable.")
                skipped_fatal += 1
                continue

            # Lọc theo --error-type
            if error_type_filter and error_type_filter.lower() not in etype.lower():
                skipped_filter += 1
                continue

            # Lọc chỉ network errors nếu --replay-network
            if network_only and etype not in NETWORK_ERROR_TYPES:
                print(f"  [SKIP-NON-NET] {msg_id} error_type={etype} — skipping (not a network error).")
                skipped_filter += 1
                continue

            raw_data = fields.get("data", "{}")
            trace_id = fields.get("trace_id") or error_ctx.get("trace_id", f"dlq-replay-{int(time.time())}")

            if dry_run:
                print(f"  [DRY-RUN] Would replay {msg_id} (error_type={etype}, trace_id={trace_id})")
                replayed += 1
                continue

            # XADD về Inbound
            await r.xadd(INBOUND_STREAM, {"data": raw_data, "_stable_id": trace_id})
            # Xóa khỏi DLQ sau khi đã đẩy thành công
            await r.xdel(DLQ_STREAM, msg_id)
            print(f"  [REPLAYED] {msg_id} → {INBOUND_STREAM} (error_type={etype}, trace_id={trace_id})")
            replayed += 1

        print(f"\n  Summary: replayed={replayed} | skipped_fatal={skipped_fatal} | skipped_filter={skipped_filter}")
        if dry_run:
            print("  ⚠️  Dry-run mode — no changes made.")
    finally:
        await r.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DLQ Replay Tool — Phòng Hồi Sức Cấp Cứu",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--list", action="store_true", help="Liệt kê tin nhắn trong DLQ")
    parser.add_argument("--replay", action="store_true", help="Replay tin nhắn theo --error-type")
    parser.add_argument("--replay-network", action="store_true", help="Replay chỉ các lỗi mạng (safe)")
    parser.add_argument("--replay-id", metavar="MSG_ID", help="Replay tin nhắn cụ thể theo Stream ID")
    parser.add_argument("--error-type", metavar="TYPE", help="Lọc theo loại lỗi (vd: HTTPStatusError)")
    parser.add_argument("--dry-run", action="store_true", help="Chỉ in ra, không thực sự replay")
    args = parser.parse_args()

    if args.list:
        asyncio.run(list_dlq(error_type_filter=args.error_type))
    elif args.replay or args.replay_network or args.replay_id:
        asyncio.run(replay_messages(
            error_type_filter=args.error_type,
            network_only=args.replay_network,
            msg_id_filter=args.replay_id,
            dry_run=args.dry_run,
        ))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
