#!/usr/bin/env python3
"""Run a Shadow OS command step and publish feedback to Kafka."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shlex
import socket
import subprocess
import time
from typing import Any

from aiokafka import AIOKafkaProducer

DEFAULT_TOPIC = os.getenv("OMNI_KAFKA_TOPIC_ACTION_FEEDBACK", "omni-action-feedback")
DEFAULT_BOOTSTRAP = os.getenv("OMNI_KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")


def _hash_command(command: str) -> str:
    return hashlib.sha256((command or "").encode("utf-8")).hexdigest()[:24]


def _run(command: str, timeout_sec: int) -> tuple[str, str, int, float]:
    started = time.monotonic()
    proc = subprocess.run(
        command,
        shell=True,
        text=True,
        capture_output=True,
        timeout=max(1, int(timeout_sec)),
        executable="/bin/bash",
    )
    elapsed = (time.monotonic() - started) * 1000.0
    return proc.stdout, proc.stderr, int(proc.returncode), elapsed


def _build_payload(args: argparse.Namespace, stdout: str, stderr: str, exit_code: int, duration_ms: float) -> dict[str, Any]:
    return {
        "trace_id": args.trace_id,
        "tool_name": "shadow_os_command",
        "correlation_id": f"{args.trace_id}:{args.step_id}",
        "stdout": stdout[:24000],
        "stderr": stderr[:12000],
        "exit_code": exit_code,
        "status": "ok" if exit_code == 0 else "error",
        "skipped_reason": "",
        "mutate_args": {
            "step_id": args.step_id,
            "command_hash": _hash_command(args.command),
            "host_identity": socket.gethostname(),
            "dry_run_command": args.dry_run_command,
            "command": args.command,
            "duration_ms": int(duration_ms),
        },
    }


async def _send(topic: str, bootstrap: str, payload: dict[str, Any]) -> None:
    producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
    await producer.start()
    try:
        await producer.send_and_wait(topic, json.dumps({"data": json.dumps(payload, ensure_ascii=False)}).encode("utf-8"))
    finally:
        await producer.stop()


def main() -> None:
    p = argparse.ArgumentParser(description="Execute shadow command and publish omni-action-feedback")
    p.add_argument("--trace-id", required=True)
    p.add_argument("--step-id", required=True)
    p.add_argument("--dry-run-command", required=True)
    p.add_argument("--command", required=True)
    p.add_argument("--timeout-sec", type=int, default=90)
    p.add_argument("--kafka-topic", default=DEFAULT_TOPIC)
    p.add_argument("--kafka-bootstrap", default=DEFAULT_BOOTSTRAP)
    p.add_argument("--skip-dry-run", action="store_true")
    args = p.parse_args()

    if not args.skip_dry_run:
        dr_out, dr_err, dr_code, _ = _run(args.dry_run_command, args.timeout_sec)
        if dr_code != 0:
            payload = _build_payload(args, dr_out, dr_err, dr_code, 0)
            payload["skipped_reason"] = "DRY_RUN_FAILED"
            asyncio.run(_send(args.kafka_topic, args.kafka_bootstrap, payload))
            raise SystemExit(dr_code)

    stdout, stderr, exit_code, duration_ms = _run(args.command, args.timeout_sec)
    payload = _build_payload(args, stdout, stderr, exit_code, duration_ms)
    asyncio.run(_send(args.kafka_topic, args.kafka_bootstrap, payload))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
