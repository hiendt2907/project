#!/usr/bin/env python3
"""
Parallel 4-lane Death Loop E2E
================================
Bắn 4 lane đồng thời qua pipeline thực:
  Lane 1 SYS_RESOURCE  — gateway POST HighCPU + 3-sigma baseline
  Lane 2 SYS_HARD_FAIL — gateway POST PodNotReady
  Lane 3 APP_HTTP      — Kafka omni-diagnostic-evidence (HTTP surge)
  Lane 4 SIEM_SECURITY — Redis xadd stream:actionable_incidents

Death-loop per lane:
  1) Inject signal
  2) Chờ SUGGEST_REMEDIATION / advisory_analyst_ok trong pod logs
  3) Publish synthetic omni-action-feedback (death-loop trigger)
  4) Chờ COMMAND_FEEDBACK_INGESTED / action_feedback_received
  5) Query Loki per trace (phân tích log)

Required env: E2E_LIVE_PROFILE_JSON
Optional env: E2E_KAFKA_BOOTSTRAP, E2E_REDIS_MA_URL, E2E_REDIS_FG_URL,
              E2E_GATEWAY_URL, E2E_LLM_TIMEOUT, E2E_FEEDBACK_WAIT_SEC
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

sys.stdout.reconfigure(line_buffering=True)

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))
sys.path.insert(0, os.path.join(_REPO, "scripts"))

from aiokafka import AIOKafkaProducer
from redis.asyncio import Redis

from pkg.autonomous_actions import build_action_feedback_body
from workers.baseline_snapshot import REDIS_KEY_SNAPSHOT
from e2e_live_profile import (
    E2EProfileError,
    load_json_file,
    load_live_e2e_profile,
    resolved_path,
    substitute_placeholders,
)

# ── constants ─────────────────────────────────────────────────────────────────

NS = "multi-agent"
LOKI_URL = "http://loki.monitor.svc.cluster.local:3100"
TOPIC_FEEDBACK = "omni-action-feedback"
LOKI_POD_RE = "omni-prober.*|omni-analyst.*|omni-core.*|omni-executor.*|omni-gateway.*"

ADV_MARKERS = [
    "SUGGEST_REMEDIATION",
    "advisory_analyst_ok",
    "advisory_analyst_null",
    "diag_batch_flush",
]
FEEDBACK_MARKERS = [
    "COMMAND_FEEDBACK_INGESTED",
    "action_feedback_received",
    "transition=COMMAND_FEEDBACK_INGESTED",
]
SIEM_MARKERS = ["siem_suggest_only_emitted", "SIEM_SUGGEST_ONLY"]

# ── kubectl ───────────────────────────────────────────────────────────────────


def _kube_cmd() -> list[str]:
    wk = os.path.join(_REPO, "scripts", "with_working_kube.sh")
    if os.path.isfile(wk):
        return [wk, "kubectl"]
    return ["kubectl"]


def _kubectl(*args: str, timeout: int = 15) -> str:
    try:
        return subprocess.check_output(_kube_cmd() + list(args), text=True, timeout=timeout).strip()
    except Exception:
        return ""


def _svc_ip(ns: str, svc: str) -> str:
    return _kubectl("get", "svc", svc, "-n", ns, "-o", "jsonpath={.spec.clusterIP}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def banner(msg: str) -> None:
    bar = "═" * 72
    print(f"\n{bar}\n  {msg}\n{bar}")


def p_ok(msg: str) -> None:
    print(f"  [PASS] {msg}")


def p_fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def p_info(msg: str) -> None:
    print(f"  [INFO] {msg}")


# ── connections ───────────────────────────────────────────────────────────────


@dataclass
class Ctx:
    prof: dict[str, Any]
    kafka_bootstrap: str
    redis_ma_url: str
    redis_fg_url: str
    gateway_url: str
    llm_timeout: float
    feedback_wait: float
    analyst_deploy: str
    siem_bridge_deploy: str
    exec_deploy: str = "omni-prober"


def _build_ctx(prof: dict[str, Any]) -> Ctx:
    ns = prof["omni_namespace"]

    kafka_ip = _svc_ip(ns, prof["kafka_svc_name"])
    kb = os.getenv("E2E_KAFKA_BOOTSTRAP") or (f"{kafka_ip}:9092" if kafka_ip else "")
    if not kb:
        raise RuntimeError("Cannot resolve Kafka. Set E2E_KAFKA_BOOTSTRAP.")

    redis_ip = _svc_ip(ns, prof["redis_ma_svc_name"])
    rma = os.getenv("E2E_REDIS_MA_URL") or (f"redis://{redis_ip}:6379/0" if redis_ip else "")
    if not rma:
        raise RuntimeError("Cannot resolve Redis MA. Set E2E_REDIS_MA_URL.")

    rfg = os.getenv("E2E_REDIS_FG_URL") or _resolve_fg_redis(prof)
    gw_ip = _svc_ip(ns, prof["gateway_svc_name"])
    gw = os.getenv("E2E_GATEWAY_URL") or (
        f"http://{gw_ip}{prof['gateway_webhook_path']}" if gw_ip else ""
    )
    return Ctx(
        prof=prof,
        kafka_bootstrap=kb,
        redis_ma_url=rma,
        redis_fg_url=rfg,
        gateway_url=gw,
        llm_timeout=float(os.getenv("E2E_LLM_TIMEOUT", "240")),
        feedback_wait=float(os.getenv("E2E_FEEDBACK_WAIT_SEC", "18")),
        analyst_deploy=prof["analyst_deploy_name"],
        siem_bridge_deploy=prof["siem_bridge_deploy_name"],
    )


def _resolve_fg_redis(prof: dict[str, Any]) -> str:
    for ns in prof["fg_redis_namespace_order"]:
        pod = prof["fg_redis_pod_name"]
        ip = _kubectl("get", "pod", pod, "-n", ns, "-o", "jsonpath={.status.podIP}")
        if not ip:
            continue
        raw = _kubectl("get", "secret", prof["fg_redis_auth_secret_name"], "-n", ns, "-o", "jsonpath={.data.password}")
        pw = base64.b64decode(raw).decode() if raw else ""
        pw = os.getenv("E2E_REDIS_FG_PASSWORD", pw)
        auth = f":{pw}@" if pw else ""
        return f"redis://{auth}{ip}:6379/0"
    raise RuntimeError("Cannot resolve FG Redis. Set E2E_REDIS_FG_URL.")


# ── log helpers ───────────────────────────────────────────────────────────────


def _pod_logs_sync(ctx: Ctx, deploy: str, since_min: int = 30) -> str:
    return _kubectl(
        "logs", "-n", ctx.prof["omni_namespace"],
        f"deploy/{deploy}", f"--since={since_min}m", "--tail=80000",
        timeout=30,
    )


async def _pod_logs(ctx: Ctx, deploy: str, since_min: int = 30) -> str:
    return await asyncio.to_thread(_pod_logs_sync, ctx, deploy, since_min)


def _grep_trace(logs: str, trace: str) -> list[str]:
    return [ln for ln in logs.splitlines() if trace in ln]


async def _wait_logs(ctx: Ctx, trace: str, markers: list[str], timeout: float, since_min: int = 30) -> tuple[bool, list[str]]:
    """Poll pod logs until any marker appears for this trace. Fully async — never blocks the event loop."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        parts = await asyncio.gather(
            _pod_logs(ctx, ctx.analyst_deploy, since_min),
            _pod_logs(ctx, "omni-prober", since_min),
            _pod_logs(ctx, "omni-executor", since_min),
        )
        lines = _grep_trace("\n".join(parts), trace)
        for m in markers:
            if any(m in ln for ln in lines):
                return True, lines
        wait = min(6.0, deadline - time.monotonic())
        if wait > 0:
            await asyncio.sleep(wait)
    parts = await asyncio.gather(
        _pod_logs(ctx, ctx.analyst_deploy, since_min),
        _pod_logs(ctx, "omni-prober", since_min),
    )
    return False, _grep_trace("\n".join(parts), trace)


# ── Loki query ────────────────────────────────────────────────────────────────

_LOKI_PYTHON = """
import json, os, time, urllib.parse, urllib.request

trace = os.environ["TRACE"]
loki  = os.environ["LOKI_URL"]
ns    = os.environ.get("LOKI_NS", "multi-agent")
lim   = int(os.environ.get("LOKI_LIMIT", "800"))
POD_RE = "omni-prober.*|omni-analyst.*|omni-core.*|omni-executor.*|omni-gateway.*|omni-siem-bridge.*"

q = '{namespace="' + ns + '", pod_name=~"' + POD_RE + '"} |= "' + trace + '"'
now = int(time.time())
params = urllib.parse.urlencode({"query": q, "limit": str(lim),
    "start": str((now - 3600) * 10**9), "end": str(now * 10**9)})
url = loki.rstrip("/") + "/loki/api/v1/query_range?" + params

try:
    d = json.loads(urllib.request.urlopen(url, timeout=35).read())
except Exception as e:
    print(json.dumps({"loki_error": str(e)})); raise SystemExit(0)

rows = []
by_pod = {}
for s in (d.get("data") or {}).get("result") or []:
    pod = (s.get("stream") or {}).get("pod_name") or "?"
    for v in (s.get("values") or []):
        if len(v) >= 2:
            rows.append((int(v[0]), pod, v[1]))
            by_pod[pod] = by_pod.get(pod, 0) + 1

rows.sort(key=lambda x: x[0])
lines = [r[2] for r in rows]

print(json.dumps({"total": len(lines), "by_pod": by_pod,
    "tail": lines[-30:] if lines else [], "head": lines[:5] if lines else []}))
"""


def _loki_query(ctx: Ctx, trace: str) -> dict[str, Any]:
    """Query Loki via kubectl exec into omni-prober."""
    ns = ctx.prof["omni_namespace"]
    cmd = _kube_cmd() + [
        "exec", "-i", "-n", ns, f"deploy/{ctx.exec_deploy}", "--",
        "env",
        f"TRACE={trace}",
        f"LOKI_URL={LOKI_URL}",
        f"LOKI_NS={ns}",
        "LOKI_LIMIT=800",
        "python3", "-c", _LOKI_PYTHON,
    ]
    try:
        out = subprocess.check_output(cmd, text=True, timeout=50).strip()
        for line in out.splitlines():
            if line.startswith("{"):
                return json.loads(line)
    except Exception as e:
        return {"loki_error": str(e)}
    return {"loki_error": "no json output"}


# ── death loop helpers ────────────────────────────────────────────────────────


async def _publish_feedback(producer: AIOKafkaProducer, trace: str) -> None:
    body = build_action_feedback_body(
        trace_id=trace,
        tool_name="e2e_parallel_death_loop",
        correlation_id=f"{trace}:death_loop_e2e",
        stdout="",
        stderr="",
        exit_code=-1,
        status="skipped",
        skipped_reason="E2E parallel death-loop: synthetic feedback (suggest-only cluster).",
        mutate_args={"e2e_parallel": True},
    )
    raw = json.dumps({"data": json.dumps(body, ensure_ascii=False)}).encode()
    await producer.send_and_wait(TOPIC_FEEDBACK, raw)


async def _post_gateway(gateway_url: str, payload: dict) -> tuple[str, int]:
    import httpx
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(gateway_url, json=payload)
        body = resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
        return str(body.get("trace_id") or ""), resp.status_code


async def _publish_evidence(producer: AIOKafkaProducer, topic: str, trace: str, docs: list[dict]) -> None:
    for doc in docs:
        inner = {**doc, "trace_id": trace, "kind": "diagnostic_evidence"}
        envelope = {"data": json.dumps(inner, ensure_ascii=False)}
        # send_and_wait gets broker ack immediately — avoids Future pending across coroutines
        await producer.send_and_wait(topic, json.dumps(envelope).encode())


# ── Lane result ───────────────────────────────────────────────────────────────


@dataclass
class LaneResult:
    lane: int
    name: str
    stream_tag: str
    trace: str = ""
    adv_found: bool = False
    feedback_sent: bool = False
    dl_found: bool = False       # death loop confirmed
    loki: dict[str, Any] = field(default_factory=dict)
    log_lines: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    error: str = ""


# ── Lane implementations ──────────────────────────────────────────────────────


async def lane1_resource(ctx: Ctx, redis_ma: Redis, producer: AIOKafkaProducer) -> LaneResult:
    r = LaneResult(lane=1, name="SYS_RESOURCE (3-sigma CPU/mem)", stream_tag="SYS_RESOURCE")
    p = ctx.prof
    try:
        snap = dict(load_json_file(resolved_path(p, "baseline_snapshot")))
        snap["ts"] = now_iso()
        await redis_ma.set(REDIS_KEY_SNAPSHOT, json.dumps(snap))
        await redis_ma.expire(REDIS_KEY_SNAPSHOT, 3600)
        p_info("[L1] baseline snapshot set (z_cpu=4.8, z_mem=3.5)")

        tmpl = load_json_file(resolved_path(p, "lane1_gateway_alert_template"))
        mapping = {
            "GATEWAY_RECEIVER": p["gateway_receiver_name"],
            "OMNI_NAMESPACE": p["omni_namespace"],
            "LANE1_DEPLOYMENT": p["lane1_deployment"],
            "LANE1_SUMMARY": p["lane1_summary"],
            "LANE1_DESCRIPTION": p["lane1_description"],
            "STAMP": now_iso(),
        }
        payload = substitute_placeholders(tmpl, mapping)
        trace, status = await _post_gateway(ctx.gateway_url, payload)
        r.trace = trace or p["trace_fallback_lane1"]
        p_info(f"[L1] gateway POST → {status}  trace={r.trace}")

        adv_found, lines = await _wait_logs(ctx, r.trace, ADV_MARKERS, ctx.llm_timeout, since_min=25)
        r.adv_found = adv_found
        r.log_lines = lines
        combined = "\n".join(lines)
        if adv_found:
            p_info(f"[L1] advisory found ({len(lines)} lines)")
            await _publish_feedback(producer, r.trace)
            r.feedback_sent = True
            await asyncio.sleep(ctx.feedback_wait)
            # Extended wait: LLM may take up to 60s after diag_batch_flush. COMMAND_FEEDBACK_INGESTED
            # arrives after LLM completes. Allow 120s total.
            dl_found, dl_lines = await _wait_logs(ctx, r.trace, FEEDBACK_MARKERS, 120, since_min=25)
            r.dl_found = dl_found
            r.log_lines = lines + dl_lines
            combined = "\n".join(r.log_lines)
        else:
            p_fail("[L1] advisory timeout — skipping feedback step")

        r.checks = {
            "baseline set in Redis": True,
            "gateway POST 200": status == 200,
            "advisory marker found": adv_found,
            "death loop feedback sent": r.feedback_sent,
            "COMMAND_FEEDBACK_INGESTED": r.dl_found,
            "3-SIGMA RESOURCE BASELINE in logs": "sigma_baseline_injected" in combined,
            "SUGGEST_REMEDIATION in logs": "SUGGEST_REMEDIATION" in combined,
            "diag_batch_flush in logs": "diag_batch_flush" in combined,
        }
    except Exception as e:
        r.error = str(e)
        p_fail(f"[L1] exception: {e}")
    return r


async def lane2_system_errors(ctx: Ctx, producer: AIOKafkaProducer) -> LaneResult:
    r = LaneResult(lane=2, name="SYS_HARD_FAIL (AnalystAdvisory)", stream_tag="SYS_HARD_FAIL")
    p = ctx.prof
    try:
        tmpl = load_json_file(resolved_path(p, "lane2_gateway_alert_template"))
        mapping = {
            "GATEWAY_RECEIVER": p["gateway_receiver_name"],
            "OMNI_NAMESPACE": p["omni_namespace"],
            "LANE2_DEPLOYMENT": p["lane2_deployment"],
            "LANE2_POD": p["lane2_pod"],
            "LANE2_SUMMARY": p["lane2_summary"],
            "LANE2_DESCRIPTION": p["lane2_description"],
            "STAMP": now_iso(),
        }
        payload = substitute_placeholders(tmpl, mapping)
        trace, status = await _post_gateway(ctx.gateway_url, payload)
        r.trace = trace or p["trace_fallback_lane2"]
        p_info(f"[L2] gateway POST → {status}  trace={r.trace}")

        adv_found, lines = await _wait_logs(ctx, r.trace, ADV_MARKERS, ctx.llm_timeout, since_min=25)
        r.adv_found = adv_found
        r.log_lines = lines
        combined = "\n".join(lines)
        if adv_found:
            p_info(f"[L2] advisory found ({len(lines)} lines)")
            await _publish_feedback(producer, r.trace)
            r.feedback_sent = True
            await asyncio.sleep(ctx.feedback_wait)
            # Extended wait: LLM may take up to 60s after diag_batch_flush. COMMAND_FEEDBACK_INGESTED
            # arrives only after LLM completes AND feedback is processed. Allow 120s total.
            dl_found, dl_lines = await _wait_logs(ctx, r.trace, FEEDBACK_MARKERS, 120, since_min=25)
            r.dl_found = dl_found
            r.log_lines = lines + dl_lines
            combined = "\n".join(r.log_lines)
        else:
            p_fail("[L2] advisory timeout — skipping feedback step")

        r.checks = {
            "gateway POST 200": status == 200,
            "advisory marker found": adv_found,
            "death loop feedback sent": r.feedback_sent,
            "COMMAND_FEEDBACK_INGESTED": r.dl_found,
            "advisory_analyst_ok in logs": "advisory_analyst_ok" in combined,
            "SUGGEST_REMEDIATION in logs": "SUGGEST_REMEDIATION" in combined,
            "audit_block_written in logs": "audit_block_written" in combined,
            "diag_batch_flush in logs": "diag_batch_flush" in combined,
            "memory/OOM signal in logs": any(
                kw in combined.lower()
                for kw in ["memory", "oom", "limit", "eviction", "omni-analyst"]
            ),
        }
    except Exception as e:
        r.error = str(e)
        p_fail(f"[L2] exception: {e}")
    return r


async def _loki_push_fake_429(ctx: Ctx, pod_name: str) -> bool:
    """Push fake 429 access-log lines to Loki so loki_access_log_surge probe finds them."""
    ns = ctx.prof["omni_namespace"]
    now_ns = int(time.time() * 1e9)
    entries = [
        [str(now_ns - i * 1_000_000_000), f'192.168.1.{i % 255} - - [06/May/2026:14:00:{i:02d} +0000] "GET /api/v1/data HTTP/1.1" 429 102 "-" "e2e-client"']
        for i in range(50)
    ]
    push_payload = json.dumps({
        "streams": [{
            "stream": {"namespace": ns, "pod_name": pod_name, "container": "nginx"},
            "values": entries,
        }]
    })
    loki_push_py = (
        "import urllib.request, sys\n"
        "req = urllib.request.Request(\n"
        "    'http://loki.monitor.svc.cluster.local:3100/loki/api/v1/push',\n"
        "    data=sys.stdin.buffer.read(), headers={'Content-Type': 'application/json'}, method='POST')\n"
        "try:\n"
        "    urllib.request.urlopen(req, timeout=10)\n"
        "    print('ok')\n"
        "except Exception as e:\n"
        "    print(f'err:{e}')\n"
    )
    cmd = _kube_cmd() + [
        "exec", "-i", "-n", ns, f"deploy/{ctx.exec_deploy}", "--",
        "python3", "-c", loki_push_py,
    ]
    try:
        out = subprocess.check_output(cmd, input=push_payload.encode(), timeout=20).decode().strip()
        return out.startswith("ok")
    except Exception as e:
        p_info(f"[L3] Loki push warning: {e}")
        return False


async def lane3_app_http(ctx: Ctx, producer: AIOKafkaProducer) -> LaneResult:
    r = LaneResult(lane=3, name="APP_HTTP (HTTP surge via gateway)", stream_tag="APP_HTTP")
    p = ctx.prof
    try:
        # Pre-populate Loki with 429 access-log lines so loki_access_log_surge probe finds them
        pod_name = f"{p['lane1_deployment']}-e2e-0"
        loki_ok = await _loki_push_fake_429(ctx, pod_name)
        p_info(f"[L3] Loki 429 pre-seed: {'ok' if loki_ok else 'skipped (Loki unreachable)'}")

        # Send Prometheus alert through gateway — triggers prober → loki_access_log_surge probe
        gw_payload = {
            "receiver": p["gateway_receiver_name"],
            "status": "firing",
            "alerts": [{
                "status": "firing",
                "labels": {
                    "alertname": "HighErrorRateSurge",
                    "severity": "warning",
                    "namespace": p["omni_namespace"],
                    "pod": pod_name,
                    "service": p.get("lane1_deployment", "nginx-test"),
                },
                "annotations": {
                    "summary": "HTTP error surge: 429 rate-limit dominant (42% of requests)",
                    "description": "Access log surge: 429x42, 499x8 in last 300s — rate_limit dominant, sigma bypass eligible",
                },
                "startsAt": now_iso(),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "",
            }],
            "groupLabels": {},
            "commonLabels": {},
            "commonAnnotations": {},
            "externalURL": "",
            "version": "4",
            "groupKey": "",
            "truncatedAlerts": 0,
        }
        trace, status = await _post_gateway(ctx.gateway_url, gw_payload)
        r.trace = trace or f"e2e-lane3-{uuid.uuid4().hex[:8]}"
        p_info(f"[L3] gateway POST → {status}  trace={r.trace}")

        adv_markers = ADV_MARKERS + ["loki_access_log_surge"]
        adv_found, lines = await _wait_logs(ctx, r.trace, adv_markers, ctx.llm_timeout, since_min=20)
        r.adv_found = adv_found
        r.log_lines = lines
        combined = "\n".join(lines)
        if adv_found:
            p_info(f"[L3] advisory found ({len(lines)} lines)")
            await _publish_feedback(producer, r.trace)
            r.feedback_sent = True
            await asyncio.sleep(ctx.feedback_wait)
            # Extended wait: LLM may take up to 60s after probe detection. COMMAND_FEEDBACK_INGESTED
            # arrives only after LLM completes AND feedback is processed. Allow 120s total.
            dl_found, dl_lines = await _wait_logs(ctx, r.trace, FEEDBACK_MARKERS, 120, since_min=20)
            r.dl_found = dl_found
            # Brief extra sleep + refresh to catch SUGGEST_REMEDIATION from executor which may lag
            # by a second or two after COMMAND_FEEDBACK_INGESTED appears in analyst logs.
            await asyncio.sleep(5)
            _, extra_lines = await _wait_logs(ctx, r.trace, ["SUGGEST_REMEDIATION", "advisory_analyst_ok"], 30, since_min=20)
            r.log_lines = lines + dl_lines + extra_lines
            combined = "\n".join(r.log_lines)
        else:
            p_fail("[L3] advisory timeout")

        from workers.log_surge_probe import classify_http_status
        bypass_ok = all(
            classify_http_status(row["status"]) == row["expected_class"]
            for row in p["lane3_http_classify_expectations"]
        )
        r.checks = {
            "gateway POST 200": status == 200,
            "loki 429 pre-seed": loki_ok,
            "HTTP classify logic correct (5 cases)": bypass_ok,
            "advisory marker found": adv_found,
            "loki_access_log_surge probe in logs": "loki_access_log_surge" in combined,
            "death loop feedback sent": r.feedback_sent,
            "COMMAND_FEEDBACK_INGESTED": r.dl_found,
            "SUGGEST_REMEDIATION in logs": "SUGGEST_REMEDIATION" in combined,
            "rate_limit / 429 in logs": ("rate_limit" in combined.lower() or "429" in combined),
        }
    except Exception as e:
        r.error = str(e)
        p_fail(f"[L3] exception: {e}")
    return r


async def lane4_siem(ctx: Ctx, redis_fg: Redis, producer: AIOKafkaProducer) -> LaneResult:
    r = LaneResult(lane=4, name="SIEM_SECURITY (kill-chain forecast)", stream_tag="SIEM_SECURITY")
    p = ctx.prof
    try:
        rh = int(p["siem_random_hex_len"])
        suffix = uuid.uuid4().hex[:rh]
        incident_id = f'{p["siem_incident_id_prefix"]}{suffix}'
        r.trace = f'{p["siem_trace_prefix"]}{suffix[:int(p["siem_trace_body_hex_len"])]}'

        tmpl_row = load_json_file(resolved_path(p, "siem_redis_xadd_row_template"))
        mapping = _siem_mapping(p, incident_id, r.trace)
        siem_fields = substitute_placeholders(tmpl_row, mapping)
        msg_id = await redis_fg.xadd(p["siem_redis_stream"], siem_fields)
        p_info(f"[L4] xadd incident_id={incident_id} trace={r.trace} msg_id={msg_id}")

        # siem-bridge rewrites trace_id to fg-e2e-siem; search by incident_id instead
        siem_markers = SIEM_MARKERS + [incident_id]
        adv_found, lines = await _wait_logs(ctx, incident_id, siem_markers, ctx.llm_timeout, since_min=30)
        r.adv_found = adv_found
        r.log_lines = lines

        if not adv_found:
            bridge_raw = await _pod_logs(ctx, ctx.siem_bridge_deploy, since_min=15)
            bridge_lines = [ln for ln in bridge_raw.splitlines() if incident_id in ln or r.trace in ln]
            r.log_lines = lines + bridge_lines
            p_info(f"[L4] bridge log hits: {len(bridge_lines)}")

        combined = "\n".join(r.log_lines)

        if adv_found:
            p_info(f"[L4] SIEM marker found ({len(lines)} lines)")
            # Feedback on the bridge's canonical trace (fg-e2e-siem) not our injected trace
            canonical_trace = next((ln.split("trace_id=")[-1].split()[0] for ln in lines if "trace_id=" in ln), r.trace)
            await _publish_feedback(producer, canonical_trace)
            r.feedback_sent = True
            await asyncio.sleep(ctx.feedback_wait)
            dl_found, dl_lines = await _wait_logs(ctx, canonical_trace, FEEDBACK_MARKERS, 30)
            r.dl_found = dl_found
            r.log_lines = lines + dl_lines
            combined = "\n".join(r.log_lines)
        else:
            p_fail(f"[L4] SIEM marker timeout (incident_id={incident_id})")

        from workers.evidence_consumer import _siem_diagnosis_from_batch, _siem_forecast_timeline, _format_siem_forecast_text
        tmpl_batch = load_json_file(resolved_path(p, "lane4_analyst_siem_evidence_batch_template"))
        mapping["LANE4_CANONICAL_JSON"] = json.dumps({"labels": {
            "siem_source": p["siem_source"], "siem_category": p["siem_category"],
            "siem_incident_id": incident_id, "severity": p["siem_severity"],
            "namespace": p["siem_k8s_namespace"],
        }})
        batch = substitute_placeholders(tmpl_batch, mapping)
        diag = _siem_diagnosis_from_batch(batch, {"siem_category": p["siem_category"],
            "siem_source": p["siem_source"], "siem_incident_id": incident_id,
            "severity": p["siem_severity"], "namespace": p["siem_k8s_namespace"]}, "")
        forecast = _format_siem_forecast_text(_siem_forecast_timeline(p["siem_category"], p["siem_severity"]))

        for needle in p["lane4_diag_substrings_required"]:
            r.checks[f"diag: {needle}"] = needle in diag
        r.checks["forecast +1h..+24h"] = ("+1h" in forecast and "+24h" in forecast)
        r.checks["incident_id in diag"] = incident_id in diag
        r.checks["SIEM marker in cluster logs"] = adv_found
        r.checks["death loop feedback sent"] = r.feedback_sent
        r.checks["COMMAND_FEEDBACK_INGESTED"] = r.dl_found
    except Exception as e:
        r.error = str(e)
        p_fail(f"[L4] exception: {e}")
    return r


def _siem_mapping(p: dict[str, Any], incident_id: str, trace: str) -> dict[str, str]:
    return {
        "INCIDENT_ID": incident_id,
        "TRACE": trace,
        "TIMESTAMP": now_iso(),
        "SIEM_CATEGORY": p["siem_category"],
        "SIEM_SEVERITY": p["siem_severity"],
        "SIEM_TENANT_ID": p["siem_tenant_id"],
        "SIEM_SOURCE": p["siem_source"],
        "SIEM_AFFECTED_IP": p["siem_affected_ip"],
        "SIEM_DESCRIPTION": p["siem_description"],
        "SIEM_SUGGESTED_ACTION": p["siem_suggested_action"],
        "SIEM_HITL_REQUIRED": p["siem_hitl_required"],
        "SIEM_ALERT_RULE": p["siem_alert_rule"],
        "SIEM_ALERT_HINT": p["siem_alert_hint"],
        "SIEM_K8S_NAMESPACE": p["siem_k8s_namespace"],
    }


# ── Loki analysis ─────────────────────────────────────────────────────────────


def _analyze_loki(loki: dict[str, Any], lane: int, trace: str) -> list[str]:
    if "loki_error" in loki:
        return [f"  Loki error: {loki['loki_error']}"]
    total = loki.get("total", 0)
    by_pod = loki.get("by_pod", {})
    tail = loki.get("tail", [])
    lines = [
        f"  Loki: {total} log lines indexed for trace={trace}",
    ]
    if by_pod:
        lines.append("  Log hits per pod:")
        for pod, n in sorted(by_pod.items(), key=lambda x: -x[1]):
            short = pod[:60]
            lines.append(f"    {short}: {n}")
    if tail:
        lines.append(f"  Tail (last {min(8, len(tail))} lines):")
        for ln in tail[-8:]:
            try:
                j = json.loads(ln)
                msg = j.get("message", "") or j.get("msg", "")
                logger = j.get("logger", "")
                lines.append(f"    [{logger}] {msg[:120]}")
            except Exception:
                lines.append(f"    {ln[:120]}")
    else:
        lines.append("  (no Loki lines — Promtail may not have shipped yet)")
    return lines


# ── report ────────────────────────────────────────────────────────────────────


def print_report(results: list[LaneResult]) -> None:
    banner("FINAL REPORT — 4 Lanes Parallel Death Loop")
    total_pass = 0
    for r in results:
        all_checks = all(r.checks.values()) if r.checks else False
        full_pass = r.adv_found and r.feedback_sent and all_checks
        if full_pass:
            total_pass += 1
        status = "PASS" if full_pass else "FAIL"
        print(f"\n  Lane {r.lane} [{status}] — {r.name}")
        print(f"    stream_tag={r.stream_tag}  trace={r.trace}")
        print(f"    advisory={'FOUND' if r.adv_found else 'TIMEOUT'}  "
              f"feedback={'SENT' if r.feedback_sent else 'NOT_SENT'}  "
              f"death_loop={'CONFIRMED' if r.dl_found else 'NOT_CONFIRMED'}")
        if r.error:
            print(f"    ERROR: {r.error}")
        for label, passed in r.checks.items():
            mark = "✓" if passed else "✗"
            print(f"    {mark} {label}")
        if r.loki:
            print()
            for ln in _analyze_loki(r.loki, r.lane, r.trace):
                print(ln)

    bar = "═" * 72
    print(f"\n{bar}")
    print(f"  RESULT: {total_pass}/{len(results)} lanes passed death loop")
    print(f"{bar}\n")


def print_findings(results: list[LaneResult]) -> None:
    banner("FINDINGS & PHƯƠNG ÁN TIẾP THEO")
    failures: list[tuple[int, str, str]] = []
    for r in results:
        if not r.adv_found:
            failures.append((r.lane, r.name, "advisory timeout — pipeline blocked before analyst"))
        if r.adv_found and not r.dl_found:
            failures.append((r.lane, r.name, "death loop not confirmed — COMMAND_FEEDBACK_INGESTED missing"))
        for label, passed in r.checks.items():
            if not passed:
                failures.append((r.lane, r.name, label))

    if not failures:
        print("\n  Tất cả 4 lane đều pass death loop đầy đủ.\n")
        print("  Đề xuất:")
        print("  1. Tăng E2E_LLM_TIMEOUT → 120s để chạy nhanh hơn trong CI")
        print("  2. Wire thực omni-executor EXECUTE_MUTATE → test mutation lane")
        print("  3. Thêm alert rule cho HITL_PENDING (lane nào trigger HITL?)")
        print("  4. Chạy multi-iteration (death loop × 3 lần / lane) — mô phỏng churn")
    else:
        print(f"\n  {len(failures)} vấn đề:\n")
        for ln, name, label in failures:
            print(f"  Lane {ln} ({name}): ✗ {label}")
        print()
        print("  Phân tích và đề xuất:")
        lane_fails = {r.lane for r, _, _ in [(r, n, l) for r in results for n, l in [(r.name, lbl) for lbl in []]]}
        adv_fail_lanes = [r.lane for r in results if not r.adv_found]
        dl_fail_lanes  = [r.lane for r in results if r.adv_found and not r.dl_found]

        if adv_fail_lanes:
            print(f"\n  [ADVISORY TIMEOUT — Lane {adv_fail_lanes}]")
            print("  Nguyên nhân có thể:")
            print("  • Kafka consumer group lag: omni-analyst chưa consume evidence batch")
            print("  • evidence_consumer batch flush: single-probe lane cần đủ 2 probe hoặc elapsed>=3s")
            print("  • Ollama LLM slow: qwen3.6 trả về sau >240s (tăng E2E_LLM_TIMEOUT=360)")
            print("  Khuyến nghị: kubectl logs -n multi-agent deploy/omni-analyst --since=5m | grep diag_batch")

        if dl_fail_lanes:
            print(f"\n  [DEATH LOOP KHÔNG XÁC NHẬN — Lane {dl_fail_lanes}]")
            print("  omni-action-feedback đã publish nhưng analyst chưa log COMMAND_FEEDBACK_INGESTED")
            print("  • Kiểm tra kafka_action_feedback_loop consumer group đang chạy không")
            print("  • Tăng E2E_FEEDBACK_WAIT_SEC=30 để cho analyst đủ thời gian consume")

        for r in results:
            if "advisory_analyst_ok" not in "\n".join(r.log_lines) and r.adv_found:
                print(f"\n  [Lane {r.lane}] advisory_analyst_ok không có → LLM trả null/empty")
                print("  • Xem xét advisory schema validation — AnalystAdvisory schema có đủ required fields?")
                print("  • Kiểm tra Ollama response: kubectl exec -n multi-agent deploy/omni-prober -- curl http://ollama:11434/api/tags")

            if "audit_block_written" not in "\n".join(r.log_lines) and r.lane == 2:
                print("\n  [Lane 2] CRAT audit_block_written không có")
                print("  • CRAT fail-closed: nếu Ed25519 key path sai → block toàn bộ SUGGEST_REMEDIATION")
                print("  • Kiểm tra: kubectl get secret -n multi-agent | grep audit")


# ── main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    banner(f"Omni — Parallel 4-Lane Death Loop\n  {now_iso()}")

    try:
        prof = load_live_e2e_profile(_REPO)
    except E2EProfileError as e:
        print(f"[FATAL] {e}")
        sys.exit(2)

    ctx = _build_ctx(prof)
    p_info(f"Kafka       : {ctx.kafka_bootstrap}")
    p_info(f"Redis MA    : {ctx.redis_ma_url}")
    p_info(f"Redis FG    : {ctx.redis_fg_url.split('@')[-1]}")
    p_info(f"Gateway     : {ctx.gateway_url}")
    p_info(f"LLM timeout : {ctx.llm_timeout:.0f}s per lane (parallel)")
    p_info(f"Feedback wait: {ctx.feedback_wait:.0f}s")

    redis_ma = Redis.from_url(ctx.redis_ma_url, decode_responses=True)
    redis_fg = Redis.from_url(ctx.redis_fg_url, decode_responses=True)
    producer = AIOKafkaProducer(bootstrap_servers=ctx.kafka_bootstrap)
    await producer.start()

    try:
        banner("FIRING all 4 lanes in parallel")
        p_info("Lane 1: gateway POST HighCPU (SYS_RESOURCE)")
        p_info("Lane 2: gateway POST PodNotReady (SYS_HARD_FAIL)")
        p_info("Lane 3: Kafka evidence publish HTTP surge (APP_HTTP)")
        p_info("Lane 4: Redis xadd SIEM DDoS (SIEM_SECURITY)")

        results: tuple[LaneResult, ...] = await asyncio.gather(
            lane1_resource(ctx, redis_ma, producer),
            lane2_system_errors(ctx, producer),
            lane3_app_http(ctx, producer),
            lane4_siem(ctx, redis_fg, producer),
        )
    finally:
        await producer.stop()
        await redis_ma.aclose()
        await redis_fg.aclose()

    result_list = list(results)

    banner("QUERYING Loki per lane")
    for r in result_list:
        if r.trace:
            p_info(f"Lane {r.lane}: Loki query trace={r.trace}")
            r.loki = await asyncio.to_thread(_loki_query, ctx, r.trace)
        else:
            p_info(f"Lane {r.lane}: no trace — skip Loki")

    print_report(result_list)
    print_findings(result_list)

    passed = sum(1 for r in result_list if r.adv_found and r.feedback_sent and all(r.checks.values()))
    sys.exit(0 if passed == len(result_list) else 1)


if __name__ == "__main__":
    asyncio.run(main())
