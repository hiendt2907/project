#!/usr/bin/env python3
"""
Agentic + dual-RAG chaos validation — phá lab càng mạnh càng tốt (runbook).

Chạy từ máy dev (cần kubectl + Redis tới cluster) hoặc trong pod (set OMNI_REDIS_URL).

**Trên máy host:** DNS cluster (``redis``, ``redis-cluster-*.svc``) **không resolve** — chọn một:

  - **Standalone / một cổng PF:** ``--redis-url redis://127.0.0.1:6379/0`` (mặc định tắt ``redis_cluster``).

  - **Redis Cluster (đúng như omni-worker):** bật ``--redis-cluster`` và **CSV** node tới localhost sau khi PF từng pod (hoặc tunnel tương đương), ví dụ::
      ``--redis-cluster --redis-cluster-nodes "127.0.0.1:6379,127.0.0.1:6380,..."``
    Hoặc ``export OMNI_REDIS_CLUSTER_NODES=...`` (cùng format) rồi ``--redis-cluster`` không cần lặp CSV nếu env đã đúng.

Các phase (stdout: mỗi dòng một JSON):
  - lab_apply / lab_wait / fault_* / flood_* / cb_trip / audit_sim / teardown

Ví dụ:
  OMNI_REDIS_URL=redis://127.0.0.1:6379/0  # sau kubectl port-forward
  ./scripts/with_working_kube.sh config view --minify
  python scripts/agentic_chaos_validation.py --intensity 9

  # Chỉ flood (không kubectl) — CI / không RBAC:
  python scripts/agentic_chaos_validation.py --skip-kubectl --intensity 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

WITH_KUBE = _ROOT / "scripts" / "with_working_kube.sh"

# --- Embedded manifests (tách khỏi multi-agent để không đụng workload production) ---
NS = "omni-chaos-lab"

MANIFEST_ALL = f"""
apiVersion: v1
kind: Namespace
metadata:
  name: {NS}
  labels:
    chaos.omni: "true"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: chaos-nginx
  namespace: {NS}
  labels:
    app: chaos-nginx
spec:
  replicas: 2
  selector:
    matchLabels:
      app: chaos-nginx
  template:
    metadata:
      labels:
        app: chaos-nginx
    spec:
      containers:
        - name: nginx
          image: nginx:1.25-alpine
          resources:
            requests:
              cpu: 5m
              memory: 32Mi
            limits:
              cpu: 200m
              memory: 128Mi
          ports:
            - containerPort: 80
---
apiVersion: v1
kind: Service
metadata:
  name: chaos-nginx
  namespace: {NS}
spec:
  selector:
    app: chaos-nginx
  ports:
    - port: 80
      targetPort: 80
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: chaos-crashloop
  namespace: {NS}
  labels:
    app: chaos-crashloop
spec:
  replicas: 2
  selector:
    matchLabels:
      app: chaos-crashloop
  template:
    metadata:
      labels:
        app: chaos-crashloop
    spec:
      containers:
        - name: crash
          image: busybox:1.36
          command: ["sh", "-c", "echo chaos-crash && exit 1"]
          resources:
            requests:
              cpu: 5m
              memory: 16Mi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: chaos-mock-api
  namespace: {NS}
  labels:
    app: chaos-mock-api
spec:
  replicas: 1
  selector:
    matchLabels:
      app: chaos-mock-api
  template:
    metadata:
      labels:
        app: chaos-mock-api
    spec:
      containers:
        - name: echo
          image: hashicorp/http-echo:0.2.3
          args:
            - "-listen=:8080"
            - "-text=chaos-mock-ok"
          ports:
            - containerPort: 8080
          resources:
            requests:
              cpu: 5m
              memory: 32Mi
---
apiVersion: v1
kind: Service
metadata:
  name: chaos-mock-api
  namespace: {NS}
spec:
  selector:
    app: chaos-mock-api
  ports:
    - port: 8080
      targetPort: 8080
"""


def _build_redis_settings_for_chaos(args: argparse.Namespace) -> Any:
    """WorkerSettings từ env; override cho chạy chaos trên host (standalone URL)."""
    from workers.settings import WorkerSettings

    settings = WorkerSettings()
    url = (getattr(args, "redis_url", None) or "").strip()
    want_cluster = bool(getattr(args, "redis_cluster", False))
    nodes_csv = (getattr(args, "redis_cluster_nodes", None) or "").strip()

    updates: dict[str, Any] = {}
    if url:
        updates["redis_url"] = url
    elif want_cluster and nodes_csv:
        first = nodes_csv.split(",")[0].strip()
        if first:
            if "://" in first:
                updates["redis_url"] = first
            else:
                updates["redis_url"] = f"redis://{first}/0"

    if updates:
        settings = settings.model_copy(update=updates)
    return settings


def _redis_url_safe(url: str) -> str:
    """Ẩn password nếu có — chỉ để log."""
    u = (url or "").strip()
    if "@" in u and "://" in u:
        head, _, tail = u.partition("://")
        if "@" in tail:
            creds, _, hostpart = tail.rpartition("@")
            return f"{head}://***@{hostpart}"
    return u


def _log(
    phase: str,
    action: str,
    *,
    trace_id: str,
    chaos_level: int,
    **extra: Any,
) -> None:
    row: dict[str, Any] = {
        "phase": phase,
        "action": action,
        "trace_id": trace_id,
        "timestamp": time.time(),
        "chaos_level": chaos_level,
        **extra,
    }
    print(json.dumps(row, ensure_ascii=False), flush=True)


def _kubectl(stdin: str | None, *args: str) -> tuple[int, str, str]:
    cmd = [str(WITH_KUBE), *args]
    kw: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": 180,
        "cwd": str(_ROOT),
    }
    if stdin is not None:
        kw["input"] = stdin
    r = subprocess.run(cmd, **kw)
    return r.returncode, r.stdout, r.stderr


def _alert_body(trace_id: str, chaos_tag: str) -> dict[str, Any]:
    return {
        "status": "firing",
        "receiver": "omni-chaos-validation",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "ChaosLabAlert",
                    "instance": f"chaos-{chaos_tag}",
                    "severity": "critical",
                    "namespace": NS,
                },
                "annotations": {
                    "summary": f"Synthetic chaos flood trace_id={trace_id}",
                    "description": (
                        f"omni-chaos-validation pod/nginx/crashloop mock-api — tag={chaos_tag}"
                    ),
                },
            }
        ],
    }


def _anomaly_payload(trace_id: str, i: int) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "rule_name": "PrometheusProactiveThreshold",
        "target": "cluster",
        "namespace": NS,
        "metric_value": float(random.randint(900, 9999)) / 100.0,
        "threshold": 0.0,
        "canonical_query": f'sum(kube_pod_status_phase{{namespace=~"{NS}"}}) + {i}',
        "timestamp": str(int(time.time())),
    }


async def _flood_gateway(
    client: httpx.AsyncClient,
    url: str,
    n: int,
    chaos_level: int,
    run_id: str,
) -> tuple[int, int]:
    ok = 0
    fail = 0

    async def one(i: int) -> None:
        nonlocal ok, fail
        tid = f"{run_id}-gw-{i}-{uuid.uuid4().hex[:8]}"
        try:
            r = await client.post(url, json=_alert_body(tid, f"b{i}"))
            if r.status_code < 400:
                ok += 1
            else:
                fail += 1
                _log(
                    "flood_gateway",
                    "response",
                    trace_id=tid,
                    chaos_level=chaos_level,
                    status_code=r.status_code,
                    body_snip=(r.text or "")[:200],
                )
        except Exception as e:
            fail += 1
            _log(
                "flood_gateway",
                "error",
                trace_id=tid,
                chaos_level=chaos_level,
                err=repr(e),
            )

    await asyncio.gather(*[one(i) for i in range(n)])
    return ok, fail


async def _flood_proactive(
    redis: Any,
    stream: str,
    n: int,
    chaos_level: int,
    run_id: str,
) -> tuple[int, int]:
    ok = 0
    fail = 0
    for i in range(n):
        tid = f"{run_id}-pr-{i}-{uuid.uuid4().hex[:8]}"
        try:
            payload = json.dumps(_anomaly_payload(tid, i), ensure_ascii=False)
            await redis.xadd(stream, {"data": payload})
            ok += 1
        except Exception as e:
            fail += 1
            _log(
                "flood_proactive",
                "xadd_fail",
                trace_id=tid,
                chaos_level=chaos_level,
                err=repr(e),
            )
    return ok, fail


async def _audit_simulation_entries(
    redis: Any,
    stream: str,
    run_id: str,
    chaos_level: int,
) -> None:
    try:
        await redis.xadd(
            stream,
            {
                "kind": "simulation",
                "phase": "summary",
                "run_id": run_id,
                "chaos_level": str(chaos_level),
                "ts": str(int(time.time())),
                "note": "agentic_chaos_validation end-of-run marker",
            },
        )
    except Exception as e:
        _log("audit_sim", "xadd_fail", trace_id=run_id, chaos_level=chaos_level, err=repr(e))


async def _run(args: argparse.Namespace) -> int:
    run_id = f"chaos-{uuid.uuid4().hex[:16]}"
    intensity = max(1, min(10, int(args.intensity)))
    chaos_level = intensity

    _log("start", "run", trace_id=run_id, chaos_level=chaos_level, argv=sys.argv)

    gw_url = (
        args.gateway_url
        or os.environ.get("OMNI_SIM_GATEWAY_URL")
        or "http://omni-gateway.multi-agent.svc.cluster.local/webhook/prometheus"
    )

    # Scale bursts by intensity
    n_gateway = min(200, 10 + intensity * 18)
    n_proactive = min(120, 5 + intensity * 12)

    if not args.skip_kubectl:
        _log("lab_apply", "kubectl_apply", trace_id=run_id, chaos_level=chaos_level)
        code, out, err = _kubectl(MANIFEST_ALL, "apply", "-f", "-")
        if code != 0:
            _log(
                "lab_apply",
                "fail",
                trace_id=run_id,
                chaos_level=chaos_level,
                stderr=err[:4000],
                stdout=out[:2000],
            )
            if not args.continue_on_lab_fail:
                return 1
        else:
            _log("lab_apply", "ok", trace_id=run_id, chaos_level=chaos_level)

        _log("lab_wait", "rollout", trace_id=run_id, chaos_level=chaos_level)
        for dep in ("chaos-nginx", "chaos-mock-api"):
            c2, o2, e2 = _kubectl(
                None,
                "rollout",
                "status",
                f"deployment/{dep}",
                "-n",
                NS,
                "--timeout=120s",
            )
            _log(
                "lab_wait",
                dep,
                trace_id=run_id,
                chaos_level=chaos_level,
                exit=c2,
                err_snip=e2[:800],
            )

        # --- Fault inject (càng intensity càng nhiều lần) ---
        faults = max(1, intensity // 3)
        for round_i in range(faults):
            tid_f = f"{run_id}-fault-{round_i}"
            _log("fault_scale", "nginx_to_zero", trace_id=tid_f, chaos_level=chaos_level, round=round_i)
            _kubectl(None, "scale", "deployment/chaos-nginx", "-n", NS, "--replicas=0")
            time.sleep(1.5)
            _log("fault_scale", "nginx_to_two", trace_id=tid_f, chaos_level=chaos_level, round=round_i)
            _kubectl(None, "scale", "deployment/chaos-nginx", "-n", NS, "--replicas=2")
            time.sleep(2.0)

            _log("fault_delete", "pods_chaos_nginx", trace_id=tid_f, chaos_level=chaos_level)
            _kubectl(
                None,
                "delete",
                "pods",
                "-n",
                NS,
                "-l",
                "app=chaos-nginx",
                "--grace-period=0",
                "--force",
            )
            time.sleep(2.0)

        _log("fault_rollout", "restart_mock_api", trace_id=run_id, chaos_level=chaos_level)
        _kubectl(None, "rollout", "restart", "deployment/chaos-mock-api", "-n", NS)
        _kubectl(
            None,
            "rollout",
            "status",
            "deployment/chaos-mock-api",
            "-n",
            NS,
            "--timeout=90s",
        )

    # Redis + flood
    from workers.redis_client import connect_redis

    settings = _build_redis_settings_for_chaos(args)
    audit_stream = args.audit_stream or "audit:simulation"

    _log(
        "redis",
        "connect",
        trace_id=run_id,
        chaos_level=chaos_level,
        redis_url_hint=_redis_url_safe(settings.redis_url),
    )
    redis_client = None
    try:
        redis_client = await connect_redis(settings)
        await redis_client.ping()
    except Exception as e:
        _log(
            "redis",
            "connect_fail",
            trace_id=run_id,
            chaos_level=chaos_level,
            err=repr(e),
            hint=(
                "Host: (1) standalone — port-forward một cổng + --redis-url redis://127.0.0.1:6379/0. "
                "(2) Nhiều node — dùng --redis-cluster-nodes host:6379,... (đầu tiên được dùng làm URL). "
                "DNS redis.* chỉ resolve trong cluster."
            ),
        )
        raise SystemExit(2) from e

    try:
        # Circuit breaker trip — gateway trả 503 (rồi gỡ)
        if not args.skip_circuit_breaker and intensity >= 5:
            cb_key = "omni:circuit_breaker:active"
            _log("cb_trip", "set", trace_id=run_id, chaos_level=chaos_level, key=cb_key)
            await redis_client.set(cb_key, "1", ex=120)
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
                try:
                    r = await client.post(
                        gw_url,
                        json=_alert_body(run_id + "-cb", "circuit"),
                    )
                    _log(
                        "cb_trip",
                        "gateway_probe",
                        trace_id=run_id,
                        chaos_level=chaos_level,
                        status_code=r.status_code,
                    )
                except Exception as e:
                    _log("cb_trip", "probe_error", trace_id=run_id, chaos_level=chaos_level, err=repr(e))
            await redis_client.delete(cb_key)
            _log("cb_trip", "cleared", trace_id=run_id, chaos_level=chaos_level)

        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0)) as client:
            _log(
                "flood_gateway",
                "start",
                trace_id=run_id,
                chaos_level=chaos_level,
                n=n_gateway,
                url=gw_url,
            )
            g_ok, g_fail = await _flood_gateway(client, gw_url, n_gateway, chaos_level, run_id)
            _log(
                "flood_gateway",
                "done",
                trace_id=run_id,
                chaos_level=chaos_level,
                ok=g_ok,
                fail=g_fail,
            )

        pr_stream = settings.stream_incidents_proactive
        _log(
            "flood_proactive",
            "start",
            trace_id=run_id,
            chaos_level=chaos_level,
            n=n_proactive,
            stream=pr_stream,
        )
        p_ok, p_fail = await _flood_proactive(
            redis_client, pr_stream, n_proactive, chaos_level, run_id
        )
        _log(
            "flood_proactive",
            "done",
            trace_id=run_id,
            chaos_level=chaos_level,
            ok=p_ok,
            fail=p_fail,
        )

        await _audit_simulation_entries(redis_client, audit_stream, run_id, chaos_level)
        _log("audit_sim", "stream", trace_id=run_id, chaos_level=chaos_level, stream=audit_stream)

    finally:
        if redis_client is not None:
            await redis_client.aclose()

    if not args.skip_kubectl and args.teardown:
        _log("teardown", "delete_namespace", trace_id=run_id, chaos_level=chaos_level, namespace=NS)
        code, _, err = _kubectl(None, "delete", "namespace", NS, "--wait=false")
        _log(
            "teardown",
            "exit",
            trace_id=run_id,
            chaos_level=chaos_level,
            code=code,
            err=err[:1500],
        )

    _log("done", "complete", trace_id=run_id, chaos_level=chaos_level)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Chaos validation — lab + fault + flood + audit.")
    p.add_argument("--intensity", type=int, default=7, help="1–10: burst size + fault rounds (default 7).")
    p.add_argument("--gateway-url", default="", dest="gateway_url")
    p.add_argument("--skip-kubectl", action="store_true", help="Chỉ flood Redis/gateway — không tạo namespace/workload.")
    p.add_argument(
        "--continue-on-lab-fail",
        action="store_true",
        help="kubectl apply fail vẫn chạy flood (để test từ máy không RBAC).",
    )
    p.add_argument(
        "--skip-circuit-breaker",
        action="store_true",
        help="Không SET circuit breaker (tránh 503 có chủ đích).",
    )
    p.add_argument(
        "--teardown",
        action="store_true",
        help="Xoá namespace sau khi chạy (mặc định giữ lab để sếp soi).",
    )
    p.add_argument(
        "--audit-stream",
        default="audit:simulation",
        dest="audit_stream",
        help="Redis stream cho marker simulation (default audit:simulation).",
    )
    p.add_argument(
        "--redis-url",
        default="",
        dest="redis_url",
        help=(
            "Override OMNI_REDIS_URL. Không kèm --redis-cluster → coi là standalone (một node PF)."
        ),
    )
    p.add_argument(
        "--redis-cluster",
        action="store_true",
        dest="redis_cluster",
        help="Dùng node đầu tiên trong --redis-cluster-nodes làm redis:// (standalone client).",
    )
    p.add_argument(
        "--redis-cluster-nodes",
        default="",
        dest="redis_cluster_nodes",
        help='CSV host:port, ví dụ 127.0.0.1:6379,127.0.0.1:6380 (sau port-forward từng pod).',
    )
    args = p.parse_args()
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        print(json.dumps({"phase": "abort", "reason": "KeyboardInterrupt"}, ensure_ascii=False))
        raise SystemExit(130)


if __name__ == "__main__":
    main()
