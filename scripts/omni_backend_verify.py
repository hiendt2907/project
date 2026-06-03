#!/usr/bin/env python3
"""Backend verify: Gateway HTTP webhook (not broker inject) + trace-scoped DLQ check + optional gateway metrics.

Designed for Kubernetes Job (PYTHONPATH=/app/src, multi-agent-system image) or local:
  PYTHONPATH=src .venv/bin/python scripts/omni_backend_verify.py

Cluster note: ``Service omni-gateway`` exposes **port 80 → targetPort 8000**. Use
``http://omni-gateway....svc.cluster.local/webhook/prometheus`` (default HTTP port), **not** ``:8000``.

Env / flags override defaults.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
import urllib.error
import urllib.request
from typing import Any

import httpx


def _wait_ready(url: str, timeout_sec: float, interval_sec: float) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=15) as r:
                if getattr(r, "status", 200) == 200:
                    print(f"[verify] worker ready {url}", flush=True)
                    return
        except Exception as e:
            print(f"[verify] wait ready: {e}", flush=True)
        time.sleep(max(0.5, interval_sec))
    raise SystemExit(f"worker ready timeout: {url}")


def _prometheus_webhook_body(trace_hint: str) -> dict[str, Any]:
    return {
        "receiver": "omni-backend-verify",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "OmniBackendVerify",
                    "severity": "info",
                    "trace_hint": trace_hint[:128],
                },
                "annotations": {"summary": "Omni backend verify synthetic alert"},
                "startsAt": "1970-01-01T00:00:00Z",
                "generatorURL": "http://omni-backend-verify.local/",
            }
        ],
        "externalURL": "http://omni-backend-verify.local/",
    }


def _sign_body(secret: bytes, raw: bytes) -> str:
    dig = hmac.new(secret, raw, hashlib.sha256).hexdigest()
    return f"sha256={dig}"


def _dlq_record_matches_trace(payload: dict[str, Any], trace_id: str) -> bool:
    if str(payload.get("trace_id") or "").strip() == trace_id:
        return True
    ec = payload.get("error_context")
    if isinstance(ec, str):
        try:
            inner = json.loads(ec)
            if isinstance(inner, dict) and str(inner.get("trace_id") or "").strip() == trace_id:
                return True
        except json.JSONDecodeError:
            return trace_id in ec
    return False


async def _poll_dlq_for_trace(
    *,
    bootstrap: str,
    topic: str,
    trace_id: str,
    total_sec: float,
    pre_sleep_sec: float,
) -> bool:
    """Return True if any NEW dlq message matches trace_id (failure)."""
    from aiokafka import AIOKafkaConsumer

    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap.strip(),
        group_id=f"omni-backend-verify-{uuid.uuid4().hex}",
        enable_auto_commit=False,
        auto_offset_reset="latest",
    )
    await consumer.start()
    try:
        await asyncio.sleep(max(0.0, pre_sleep_sec))
        deadline = time.monotonic() + total_sec
        while time.monotonic() < deadline:
            batch = await consumer.getmany(timeout_ms=3000)
            for _tp, records in batch.items():
                for rec in records:
                    try:
                        payload = json.loads(rec.value.decode("utf-8"))
                    except Exception:
                        continue
                    if isinstance(payload, dict) and _dlq_record_matches_trace(payload, trace_id):
                        print(f"[verify] DLQ hit trace_id={trace_id} offset={rec.offset}", flush=True)
                        return True
    finally:
        await consumer.stop()
    return False


async def _gateway_metrics_circuit_closed(metrics_url: str) -> None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(metrics_url)
        r.raise_for_status()
    found = False
    for line in r.text.splitlines():
        line = line.strip()
        if line.startswith("omni_gateway_circuit_open "):
            found = True
            val = float(line.split()[1])
            if val != 0.0:
                raise SystemExit(f"gateway circuit open (want 0): {line}")
    if not found:
        print("[verify] omni_gateway_circuit_open not in scrape (old gateway image?) — skip", flush=True)


async def _promql_instant(prometheus_url: str, query: str, max_val: float | None) -> None:
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            f"{prometheus_url.rstrip('/')}/api/v1/query",
            params={"query": query},
        )
        r.raise_for_status()
        data = r.json()
    res = data.get("data", {}).get("result") or []
    if not res:
        print(f"[verify] promql empty result for {query!r}", flush=True)
        return
    val = float(res[0]["value"][1])
    print(f"[verify] promql {query} = {val}", flush=True)
    if max_val is not None and val > max_val:
        raise SystemExit(f"promql {query}={val} exceeds {max_val}")


async def _async_main(args: argparse.Namespace) -> None:
    trace_id = args.trace_id.strip()
    if len(trace_id) < 8 or len(trace_id) > 128:
        raise SystemExit("trace_id must be 8..128 chars (gateway validation)")

    if args.worker_ready_url:
        _wait_ready(args.worker_ready_url, args.ready_timeout_sec, args.ready_interval_sec)

    # Optional: gateway must not be in CB-open scrape state
    if args.gateway_metrics_url:
        await _gateway_metrics_circuit_closed(args.gateway_metrics_url)

    body_obj = _prometheus_webhook_body(trace_id)
    raw = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-Omni-Trace-Id": trace_id,
    }
    secret = (args.webhook_secret or os.getenv("OMNI_GATEWAY_WEBHOOK_SECRET") or "").strip().encode()
    if secret:
        headers["X-Hub-Signature-256"] = _sign_body(secret, raw)

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(args.gateway_webhook_url, content=raw, headers=headers)
        print(f"[verify] webhook status={r.status_code} body={r.text[:500]}", flush=True)
        if r.status_code == 503:
            raise SystemExit("gateway returned 503 (circuit breaker or overload)")
        if r.status_code == 429:
            raise SystemExit("gateway rate limited")
        if r.status_code >= 400:
            raise SystemExit(f"webhook failed: {r.status_code}")

    if args.prometheus_url and args.promql_ttft:
        await asyncio.sleep(args.promql_sleep_sec)
        await _promql_instant(args.prometheus_url, args.promql_ttft, args.promql_ttft_max)

    if not args.skip_dlq_check:
        bootstrap = args.kafka_bootstrap or os.getenv("OMNI_KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
        topic = args.dlq_topic or os.getenv("OMNI_KAFKA_TOPIC_DLQ", "omni-dlq")
        matched = await _poll_dlq_for_trace(
            bootstrap=bootstrap,
            topic=topic,
            trace_id=trace_id,
            total_sec=args.dlq_poll_sec,
            pre_sleep_sec=args.dlq_pre_sleep_sec,
        )
        if matched:
            raise SystemExit("FAIL: DLQ contains message for this trace_id")


def main() -> None:
    p = argparse.ArgumentParser(description="Omni backend verify (Gateway HTTP + DLQ trace assert)")
    p.add_argument("--gateway-webhook-url", default=os.getenv("OMNI_VERIFY_GATEWAY_WEBHOOK_URL", "").strip())
    p.add_argument("--webhook-secret", default=os.getenv("OMNI_GATEWAY_WEBHOOK_SECRET", "").strip())
    p.add_argument("--trace-id", default=os.getenv("OMNI_VERIFY_TRACE_ID", "").strip())
    p.add_argument("--worker-ready-url", default=os.getenv("OMNI_WORKER_READY_URL", "").strip())
    p.add_argument("--ready-timeout-sec", type=float, default=float(os.getenv("OMNI_VERIFY_READY_TIMEOUT_SEC", "120")))
    p.add_argument("--ready-interval-sec", type=float, default=float(os.getenv("OMNI_VERIFY_READY_INTERVAL_SEC", "3")))
    p.add_argument("--gateway-metrics-url", default=os.getenv("OMNI_VERIFY_GATEWAY_METRICS_URL", "").strip())
    p.add_argument("--kafka-bootstrap", default=os.getenv("OMNI_KAFKA_BOOTSTRAP_SERVERS", "").strip())
    p.add_argument("--dlq-topic", default=os.getenv("OMNI_KAFKA_TOPIC_DLQ", "omni-dlq"))
    p.add_argument("--dlq-poll-sec", type=float, default=float(os.getenv("OMNI_VERIFY_DLQ_POLL_SEC", "45")))
    p.add_argument("--dlq-pre-sleep-sec", type=float, default=float(os.getenv("OMNI_VERIFY_DLQ_PRE_SLEEP_SEC", "2")))
    p.add_argument("--skip-dlq-check", action="store_true")
    p.add_argument("--prometheus-url", default=os.getenv("OMNI_VERIFY_PROMETHEUS_URL", "").strip())
    p.add_argument("--promql-ttft", default=os.getenv("OMNI_VERIFY_PROMQL_TTFT", "").strip())
    p.add_argument("--promql-ttft-max", type=float, default=float(os.getenv("OMNI_VERIFY_PROMQL_TTFT_MAX", "30")))
    p.add_argument("--promql-sleep-sec", type=float, default=float(os.getenv("OMNI_VERIFY_PROMQL_SLEEP_SEC", "15")))
    args = p.parse_args()
    if os.getenv("OMNI_VERIFY_SKIP_DLQ", "").strip().lower() in ("1", "true", "yes"):
        args.skip_dlq_check = True

    if not args.gateway_webhook_url:
        args.gateway_webhook_url = (
            os.getenv("OMNI_VERIFY_GATEWAY_WEBHOOK_URL", "").strip()
            or "http://omni-gateway.multi-agent.svc.cluster.local/webhook/prometheus"
        )
    if not args.trace_id:
        args.trace_id = f"verify-{uuid.uuid4().hex}"
    if not args.kafka_bootstrap:
        args.kafka_bootstrap = os.getenv("OMNI_KAFKA_BOOTSTRAP_SERVERS", "kafka:9092").strip()

    asyncio.run(_async_main(args))
    print("[verify] PASS", flush=True)


if __name__ == "__main__":
    main()
