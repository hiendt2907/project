#!/usr/bin/env python3
"""Compute (and optionally perform) the sleep duration until quota reset+buffer.

Stdlib-only. Reads reset_at/buffer_seconds from the loop state JSON. Never
sleeps a negative duration. See references/quota-resume-protocol.md.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _parse_iso8601(value: str) -> datetime:
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def remaining_seconds(state: dict, *, fallback_minutes: int) -> tuple[int, str]:
    quota = state.get("quota") or {}
    reset_at = quota.get("reset_at")
    buffer_seconds = int(quota.get("buffer_seconds") or 120)

    now = datetime.now(timezone.utc)
    if reset_at:
        try:
            reset_dt = _parse_iso8601(reset_at)
        except ValueError as exc:
            raise SystemExit(f"[calculate_sleep] invalid quota.reset_at={reset_at!r}: {exc}")
        remaining = (reset_dt - now).total_seconds() + buffer_seconds
        source = f"quota.reset_at={reset_at}"
    else:
        # No reset_at recorded — this is only acceptable as an explicit,
        # operator-configured fallback, never a silent guess.
        remaining = fallback_minutes * 60 + buffer_seconds
        source = f"fallback_minutes={fallback_minutes} (quota.reset_at not set)"

    return max(0, int(remaining)), source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-file",
        default="docs/operations/AUTONOMOUS_LOOP_STATE.json",
        help="Path to the state JSON file",
    )
    parser.add_argument("--fallback-minutes", type=int, default=60,
                        help="Used only when quota.reset_at is not set — must be explicit, never silent")
    parser.add_argument("--print-only", action="store_true", help="Print seconds and exit, do not sleep")
    parser.add_argument("--sleep", action="store_true", help="Actually sleep for the computed duration")
    args = parser.parse_args()

    path = Path(args.state_file)
    if not path.exists():
        print(f"[calculate_sleep] ERROR: state file not found: {path}", file=sys.stderr)
        return 2
    state = json.loads(path.read_text())

    seconds, source = remaining_seconds(state, fallback_minutes=args.fallback_minutes)
    print(f"[calculate_sleep] remaining_seconds={seconds} source={source}")

    if args.sleep and not args.print_only:
        if seconds == 0:
            print("[calculate_sleep] reset already reached — not sleeping")
            return 0
        print(f"[calculate_sleep] sleeping {seconds}s...")
        time.sleep(seconds)
        print("[calculate_sleep] wake")

    return 0


if __name__ == "__main__":
    sys.exit(main())
