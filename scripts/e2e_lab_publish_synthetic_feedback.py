#!/usr/bin/env python3
"""Publish one synthetic omni-action-feedback message (lab / suggest-only clusters).

Use when EXECUTE_MUTATE is off: proves analyst `kafka_action_feedback_loop` ingests feedback
and logs `action_feedback_received` for a trace_id without running a real mutate.

Env:
  E2E_KAFKA_BOOTSTRAP — optional; else resolve kafka ClusterIP in Omni namespace.
  E2E_OMNI_KUBE_NAMESPACE / E2E_KUBE_NS — default multi-agent.
  E2E_KUBECTL_WRAPPER — optional kubectl prefix (e.g. scripts/with_working_kube.sh).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from aiokafka import AIOKafkaProducer

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

from pkg.autonomous_actions import build_action_feedback_body  # noqa: E402


def _kube_cmd() -> list[str]:
    wrap = (os.environ.get("E2E_KUBECTL_WRAPPER") or "").strip()
    if wrap:
        return [wrap, "kubectl"]
    wk = os.path.join(_REPO_ROOT, "scripts", "with_working_kube.sh")
    if os.path.isfile(wk):
        return [wk, "kubectl"]
    return ["kubectl"]


def _kubectl(*args: str) -> str:
    try:
        return subprocess.check_output(_kube_cmd() + list(args), text=True, timeout=12).strip()
    except Exception:
        return ""


def _omni_namespace() -> str:
    return (os.environ.get("E2E_OMNI_KUBE_NAMESPACE") or os.environ.get("E2E_KUBE_NS") or "multi-agent").strip()


def _resolve_kafka() -> str:
    ns = _omni_namespace()
    ip = _kubectl("get", "svc", "kafka", "-n", ns, "-o", "jsonpath={.spec.clusterIP}")
    if not ip or ip == "None":
        raise RuntimeError("Cannot resolve Kafka ClusterIP. Set E2E_KAFKA_BOOTSTRAP.")
    return f"{ip}:9092"


async def _send(bootstrap: str, topic: str, trace_id: str) -> None:
    body = build_action_feedback_body(
        trace_id=trace_id,
        tool_name="e2e_lab_synthetic_feedback",
        correlation_id=f"{trace_id}:e2e_lab_feedback",
        stdout="",
        stderr="",
        exit_code=-1,
        status="skipped",
        skipped_reason=(
            "E2E_LAB_SYNTHETIC: omni-action-feedback ingest proof "
            "(suggest-only / no mutate; analyst death-loop channel check)."
        ),
        mutate_args={"e2e_lab": True},
    )
    producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
    await producer.start()
    try:
        raw = json.dumps({"data": json.dumps(body, ensure_ascii=False)}).encode("utf-8")
        await producer.send_and_wait(topic, raw)
    finally:
        await producer.stop()


def main() -> None:
    p = argparse.ArgumentParser(description="Publish synthetic omni-action-feedback for lab E2E")
    p.add_argument("--trace-id", required=True, help="Must match an active pipeline trace (e.g. gateway response)")
    p.add_argument(
        "--kafka-topic",
        default=os.getenv("OMNI_KAFKA_TOPIC_ACTION_FEEDBACK", "omni-action-feedback"),
    )
    p.add_argument("--kafka-bootstrap", default=os.getenv("E2E_KAFKA_BOOTSTRAP", "").strip())
    args = p.parse_args()
    bootstrap = args.kafka_bootstrap or _resolve_kafka()
    asyncio.run(_send(bootstrap, args.kafka_topic, args.trace_id.strip()))
    print(f"ok published synthetic action_feedback trace_id={args.trace_id!r} topic={args.kafka_topic!r} {bootstrap}")


if __name__ == "__main__":
    main()
