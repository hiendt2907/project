#!/usr/bin/env python3
"""Drill executor-side: bơm EXECUTE_PLAYBOOK vào omni-actions (lab-only).

Chứng minh engine E2E: gate (graduation/breaker/blast-lock) → proof-of-fault →
mutate → verify settle-loop → CRAT chain. Chạy với fault THẬT đang tồn tại
(hoặc để chứng minh PROOF_OF_FAULT_FAILED trên workload khoẻ).

Usage:
  PYTHONPATH=src .venv/bin/python scripts/drill_execute_playbook.py \
    --playbook PB-K8S-CPU-RESTART --namespace multi-agent --deployment nginx-test \
    [--kafka host:port] [--trace drill-pb-001]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time

from aiokafka import AIOKafkaProducer


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--playbook", required=True)
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--deployment", default="")
    ap.add_argument("--pod", default="")
    ap.add_argument("--tenant", default="default")
    ap.add_argument("--hitl-approved", action="store_true")
    ap.add_argument("--kafka", default=os.environ.get("E2E_KAFKA_BOOTSTRAP", "localhost:9092"))
    ap.add_argument("--trace", default=f"drill-pb-{int(time.time())}")
    args = ap.parse_args()

    render_ctx = {"namespace": args.namespace}
    if args.deployment:
        render_ctx["deployment"] = args.deployment
    if args.pod:
        render_ctx["pod"] = args.pod

    body = {
        "action": "EXECUTE_PLAYBOOK",
        "trace_id": args.trace,
        "data": {
            "playbook_id": args.playbook,
            "render_ctx": render_ctx,
            "tenant": args.tenant,
            "hitl_approved": bool(args.hitl_approved),
            "rationale": "lab drill — executor-side playbook engine E2E",
        },
    }
    producer = AIOKafkaProducer(bootstrap_servers=args.kafka)
    await producer.start()
    try:
        await producer.send_and_wait(
            "omni-actions", json.dumps({"data": json.dumps(body, ensure_ascii=False)}).encode()
        )
    finally:
        await producer.stop()
    print(f"sent EXECUTE_PLAYBOOK playbook={args.playbook} trace={args.trace} render_ctx={render_ctx}")


if __name__ == "__main__":
    asyncio.run(main())
