#!/usr/bin/env python3
"""
Simulation 15 phút — 2 luồng bám code thật:

  Luồng 1 (gateway): POST /webhook/prometheus giống Alertmanager → Kafka omni-alerts
  (src/gateway/api.py + handlers xử lý payload.data.alerts).

  Luồng 2 (proactive consumer): produce omni-proactive-incidents với JSON AnomalyEvent
  (workers/proactive_observer.py — Kafka consumer và resolve_remediation_from_memory).

Không bật “fake” Prometheus evaluate trừ khi dùng --patch-proactive-eval (kubectl patch ConfigMap).

Chạy:
  - Trong cluster: python scripts/simulate_dual_flow_15m.py
  - Từ máy: port-forward gateway + set OMNI_SIM_GATEWAY_URL=http://127.0.0.1:8080/webhook/prometheus
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiokafka import AIOKafkaProducer

from workers.redis_client import connect_redis
from workers.settings import WorkerSettings


def _alert_body() -> dict:
    """Body JSON như Prometheus/Alertmanager gửi — gateway nhét vào payload['data']."""
    return {
        "status": "firing",
        "receiver": "omni-simulation",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "SimulatedAlert",
                    "instance": "simulation:0",
                    "severity": "info",
                },
                "annotations": {
                    "summary": "Dual-flow simulation (investor demo)",
                    "description": "Synthetic alert for LGTM validation",
                },
            }
        ],
    }


def _anomaly_event_payload() -> dict:
    """Khớp workers.proactive_observer.AnomalyEvent (canonical_query tối thiểu 1 ký tự)."""
    return {
        "trace_id": f"sim-{uuid.uuid4().hex[:12]}",
        "rule_name": "PrometheusProactiveThreshold",
        "target": "cluster",
        "namespace": "",
        "metric_value": 1.0,
        "threshold": 0.0,
        "canonical_query": "sum(up)",
        "timestamp": str(int(time.time())),
    }


async def _run(args: argparse.Namespace) -> None:
    settings = WorkerSettings()
    gw_url = (
        args.gateway_url
        or os.environ.get("OMNI_SIM_GATEWAY_URL")
        or "http://omni-gateway.multi-agent.svc.cluster.local/webhook/prometheus"
    )
    deadline = time.monotonic() + float(args.duration_sec)
    interval_gw = max(5.0, float(args.interval_gateway_sec))
    interval_pr = max(30.0, float(args.interval_proactive_sec))

    next_gw = time.monotonic()
    next_pr = time.monotonic()

    redis_client = None
    kafka_p: AIOKafkaProducer | None = None
    started_epoch = int(time.time())
    if not args.gateway_only:
        redis_client = await connect_redis(settings)
        kafka_p = AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap_servers.strip())
        await kafka_p.start()
    n_gw_ok = 0
    n_gw_try = 0
    n_pr_ok = 0
    n_pr_try = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= next_gw:
                    n_gw_try += 1
                    try:
                        r = await client.post(gw_url, json=_alert_body())
                        n_gw_ok += 1
                        print(f"[gateway] POST ok #{n_gw_ok}/{n_gw_try} status={r.status_code} trace_hint={r.text[:200]!r}")
                    except Exception as e:
                        print(f"[gateway] POST fail #{n_gw_try}: {e!r}")
                    next_gw = now + interval_gw

                if kafka_p is not None and now >= next_pr:
                    n_pr_try += 1
                    try:
                        topic = settings.kafka_topic_proactive_incidents
                        payload = json.dumps(_anomaly_event_payload(), ensure_ascii=False)
                        env = json.dumps({"data": payload}, ensure_ascii=False).encode("utf-8")
                        await kafka_p.send_and_wait(topic, value=env)
                        n_pr_ok += 1
                        print(f"[proactive] Kafka ok #{n_pr_ok}/{n_pr_try} topic={topic}")
                    except Exception as e:
                        print(f"[proactive] Kafka fail #{n_pr_try}: {e!r}")
                    next_pr = now + interval_pr

                await asyncio.sleep(1.0)
    finally:
        if kafka_p is not None:
            await kafka_p.stop()
        if redis_client is not None:
            await redis_client.aclose()
        print(
            f"[audit] Kafka audit topic {settings.kafka_topic_audit_proactive} — "
            f"consume via tooling; started_epoch={started_epoch}"
        )

    print(
        f"Done: gateway_ok={n_gw_ok}/{n_gw_try} proactive_ok={n_pr_ok}/{n_pr_try} duration_sec={args.duration_sec}"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Simulate dual data paths for LGTM / demo.")
    p.add_argument("--duration-sec", type=int, default=900, dest="duration_sec", help="Default 900 = 15 minutes.")
    p.add_argument("--interval-gateway-sec", type=int, default=30, dest="interval_gateway_sec")
    p.add_argument("--interval-proactive-sec", type=int, default=180, dest="interval_proactive_sec")
    p.add_argument("--gateway-url", default="", dest="gateway_url", help="Override OMNI_SIM_GATEWAY_URL.")
    p.add_argument(
        "--gateway-only",
        action="store_true",
        help="Chỉ POST gateway (không cần Redis) — nửa luồng 2.",
    )
    args = p.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
