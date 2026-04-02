#!/usr/bin/env python3
"""Full-system audit based on live code paths.

Covers:
- API health/metrics (gateway + worker)
- Dataflow (events:inbound, incidents:proactive, audit:proactive)
- Business logic path (SOP_MISS/fallback outcomes)
- Learning metrics and action_experience size
- Grafana dashboard provisioning sanity

Usage:
  ./.venv/bin/python scripts/full_system_audit.py --duration-sec 120 --interval-sec 10 --strict
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KUBE = ROOT / "scripts" / "with_working_kube.sh"


def run(cmd: list[str], *, timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip()


def k(*args: str, timeout: int = 120) -> tuple[int, str]:
    return run([str(KUBE), *args], timeout=timeout)


def parse_metric_block(text: str, prefixes: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for ln in text.splitlines():
        for p in prefixes:
            if ln.startswith(p):
                parts = ln.split()
                if len(parts) >= 2:
                    try:
                        out[parts[0]] = float(parts[1])
                    except Exception:
                        pass
    return out


def get_worker_metrics() -> dict[str, float]:
    code = (
        "import urllib.request;"
        "t=urllib.request.urlopen('http://127.0.0.1:9090/metrics',timeout=5).read().decode();"
        "print(t)"
    )
    rc, out = k("exec", "-n", "multi-agent", "deploy/omni-worker", "--", "python", "-c", code)
    if rc != 0:
        raise RuntimeError(out)
    return parse_metric_block(
        out,
        [
            "omni_worker_messages_processed_total",
            "omni_anomaly_events_total",
            "omni_proactive_fallback_total",
            "omni_proactive_verify_total",
            "omni_learning_upserts_total",
            "omni_learning_governance_decision_total",
            "omni_learning_unique_patterns",
        ],
    )


def get_gateway_metrics() -> dict[str, float]:
    code = (
        "import urllib.request;"
        "t=urllib.request.urlopen('http://127.0.0.1:8000/metrics',timeout=5).read().decode();"
        "print(t)"
    )
    rc, out = k("exec", "-n", "multi-agent", "deploy/omni-gateway", "--", "python", "-c", code)
    if rc != 0:
        raise RuntimeError(out)
    return parse_metric_block(out, ["omni_gateway_requests_total"])


def xlen(key: str) -> int:
    rc, out = k("exec", "-n", "multi-agent", "redis-cluster-0", "--", "redis-cli", "-c", "XLEN", key)
    if rc != 0:
        raise RuntimeError(out)
    return int(out.splitlines()[-1])


def get_pgpool_pod() -> str:
    rc, out = k("get", "pods", "-n", "multi-agent")
    if rc != 0:
        raise RuntimeError(out)
    for ln in out.splitlines():
        if ln.startswith("pgpool-gateway"):
            return ln.split()[0]
    raise RuntimeError("pgpool-gateway pod not found")


def get_rag_counts() -> dict[str, int]:
    pod = get_pgpool_pod()
    pw = "GD3fjTJJxfzi0bau6TSaoWV9Q8TeuEYxahQrFDh6DCnMRjgFdEQ1q7Hf3FKFbxD8"
    query = "select collection_name,count(*) from rag_documents group by collection_name order by count(*) desc;"
    rc, out = k(
        "exec",
        "-n",
        "multi-agent",
        pod,
        "--",
        "sh",
        "-lc",
        f"PGPASSWORD='{pw}' psql -h 127.0.0.1 -U appuser -d ragdb -At -c {shlex.quote(query)}",
    )
    if rc != 0:
        raise RuntimeError(out)
    counts: dict[str, int] = {}
    total = 0
    for ln in out.splitlines():
        if "|" not in ln:
            continue
        c, n = ln.split("|", 1)
        try:
            v = int(n)
        except Exception:
            continue
        counts[c] = v
        total += v
    counts["__total__"] = total
    return counts


def get_recent_audit_outcomes(start_ts: int, count: int = 300) -> Counter:
    rc, out = k(
        "exec",
        "-n",
        "multi-agent",
        "redis-cluster-0",
        "--",
        "redis-cli",
        "-c",
        "XREVRANGE",
        "audit:proactive",
        "+",
        "-",
        "COUNT",
        str(count),
    )
    if rc != 0:
        raise RuntimeError(out)
    lines = out.splitlines()
    ctr: Counter = Counter()
    for i in range(len(lines) - 1):
        if lines[i] != "data":
            continue
        try:
            payload = json.loads(lines[i + 1])
        except Exception:
            continue
        ts = int(payload.get("ts") or 0)
        if ts < start_ts:
            continue
        ctr[str(payload.get("outcome") or "unknown")] += 1
    return ctr


def check_grafana_dashboard_marker() -> bool:
    rc, out = k(
        "get",
        "configmap",
        "grafana-dashboard-omni-ops-validation",
        "-n",
        "monitor",
        "-o",
        "json",
    )
    if rc != 0:
        return False
    try:
        data = json.loads(out)
        blob = (data.get("data") or {}).get("omni-ops-validation.json", "")
        return "Self-Learning Control Tower" in blob
    except Exception:
        return False


def simulate(duration_sec: int, interval_sec: int) -> dict[str, int]:
    gw_ok = gw_try = pr_ok = pr_try = 0
    end = time.monotonic() + duration_sec
    while time.monotonic() < end:
        gw_try += 1
        body = '{"status":"firing","alerts":[{"status":"firing","labels":{"alertname":"FullAudit","instance":"simulation"},"annotations":{"summary":"full audit"}}]}'
        rc, out = k(
            "exec",
            "-n",
            "multi-agent",
            "deploy/omni-worker",
            "--",
            "sh",
            "-lc",
            "curl -s -o /tmp/resp.txt -w '%{http_code}' -H 'Content-Type: application/json' "
            "-X POST http://omni-gateway.multi-agent.svc.cluster.local/webhook/prometheus "
            f"-d '{body}'",
        )
        code = out.splitlines()[-1] if out else "000"
        if rc == 0 and code == "200":
            gw_ok += 1

        pr_try += 1
        ts = str(int(time.time()))
        trace = f"full-audit-{ts}-{pr_try}"
        payload = (
            '{"trace_id":"'
            + trace
            + '","rule_name":"PrometheusProactiveThreshold","target":"cluster",'
            + '"namespace":"","metric_value":1.0,"threshold":0.0,"canonical_query":"sum(up)","timestamp":"'
            + ts
            + '"}'
        )
        rc2, out2 = k(
            "exec",
            "-n",
            "multi-agent",
            "redis-cluster-0",
            "--",
            "redis-cli",
            "-c",
            "XADD",
            "incidents:proactive",
            "*",
            "data",
            payload,
        )
        if rc2 == 0 and "-" in out2:
            pr_ok += 1

        time.sleep(interval_sec)
    return {
        "gateway_ok": gw_ok,
        "gateway_try": gw_try,
        "proactive_ok": pr_ok,
        "proactive_try": pr_try,
    }


def delta(after: dict[str, float], before: dict[str, float]) -> dict[str, float]:
    keys = set(after) | set(before)
    return {k: after.get(k, 0.0) - before.get(k, 0.0) for k in sorted(keys)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Full-system live audit for Omni stack")
    ap.add_argument("--duration-sec", type=int, default=120)
    ap.add_argument("--interval-sec", type=int, default=10)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--min-action-experience", type=int, default=20)
    args = ap.parse_args()

    started = int(time.time())
    before = {
        "worker_metrics": get_worker_metrics(),
        "gateway_metrics": get_gateway_metrics(),
        "streams": {
            "events:inbound": xlen("events:inbound"),
            "incidents:proactive": xlen("incidents:proactive"),
            "audit:proactive": xlen("audit:proactive"),
        },
        "rag": get_rag_counts(),
        "grafana_has_control_tower": check_grafana_dashboard_marker(),
    }

    sim = simulate(args.duration_sec, args.interval_sec)
    time.sleep(5)

    after = {
        "worker_metrics": get_worker_metrics(),
        "gateway_metrics": get_gateway_metrics(),
        "streams": {
            "events:inbound": xlen("events:inbound"),
            "incidents:proactive": xlen("incidents:proactive"),
            "audit:proactive": xlen("audit:proactive"),
        },
        "rag": get_rag_counts(),
        "audit_outcomes": dict(get_recent_audit_outcomes(started)),
        "grafana_has_control_tower": check_grafana_dashboard_marker(),
    }

    report = {
        "simulation": sim,
        "deltas": {
            "worker_metrics": delta(after["worker_metrics"], before["worker_metrics"]),
            "gateway_metrics": delta(after["gateway_metrics"], before["gateway_metrics"]),
            "streams": {k: after["streams"][k] - before["streams"][k] for k in before["streams"].keys()},
            "rag_total": after["rag"]["__total__"] - before["rag"]["__total__"],
            "rag_action_experience": after["rag"].get("action_experience", 0) - before["rag"].get(
                "action_experience", 0
            ),
        },
        "before": before,
        "after": after,
        "checks": {},
    }

    checks = {
        "gateway_api_success": sim["gateway_ok"] == sim["gateway_try"] and sim["gateway_try"] > 0,
        "proactive_injected": sim["proactive_ok"] == sim["proactive_try"] and sim["proactive_try"] > 0,
        "events_inbound_growth": report["deltas"]["streams"]["events:inbound"] >= sim["gateway_ok"],
        "incidents_proactive_growth": report["deltas"]["streams"]["incidents:proactive"] >= sim["proactive_ok"],
        "audit_proactive_growth": report["deltas"]["streams"]["audit:proactive"] > 0,
        "business_logic_audit_present": any(
            k in after["audit_outcomes"] for k in ["SOP_MISS", "FALLBACK_OK", "FALLBACK_LOW_CONFIDENCE", "LEARNING_HIT_OK"]
        ),
        "grafana_dashboard_present": bool(after["grafana_has_control_tower"]),
        "rag_action_experience_min": after["rag"].get("action_experience", 0) >= args.min_action_experience,
    }
    report["checks"] = checks
    report["summary"] = {
        "pass": all(checks.values()),
        "failed_checks": [k for k, v in checks.items() if not v],
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.strict and not report["summary"]["pass"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
