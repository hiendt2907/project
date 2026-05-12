#!/usr/bin/env python3
"""Sprint 2 live integration tests — runs against the live gateway via port-forward.

Usage:
    # Option A: direct
    python scripts/test_sprint2_live.py

    # Option B: with pycoverage (instruments gateway route modules)
    coverage run --source=src/gateway scripts/test_sprint2_live.py
    coverage report -m

Prerequisites:
    kubectl port-forward svc/omni-gateway 18000:80 -n multi-agent &
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

GW = os.getenv("OMNI_GATEWAY_URL", "http://localhost:18000")
API_KEY = os.getenv("OMNI_GATEWAY_API_KEY", "")

_PASS = 0
_FAIL = 0
_RESULTS: list[dict] = []


def _headers() -> dict:
    return {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    status = "PASS" if ok else "FAIL"
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
    _RESULTS.append({"test": name, "status": status, "detail": detail})
    colour = "\033[32m" if ok else "\033[31m"
    print(f"  {colour}{status}\033[0m  {name}" + (f"  — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n\033[1m{'─' * 50}\033[0m")
    print(f"\033[1m  {title}\033[0m")
    print(f"\033[1m{'─' * 50}\033[0m")


def main() -> int:
    client = httpx.Client(base_url=GW, headers=_headers(), timeout=15)

    # ── 1. Health ─────────────────────────────────────────────────────────────
    section("Health & Baseline")

    r = client.get("/healthz")
    check("GET /healthz → 200", r.status_code == 200, f"status={r.status_code}")
    body = r.json()
    check("healthz has status=ok", body.get("status") == "ok", str(body))

    r = client.get("/metrics")
    check("GET /metrics → 200 (Prometheus text)", r.status_code == 200, f"len={len(r.text)}")

    # ── 2. KPI ────────────────────────────────────────────────────────────────
    section("KPI Endpoints (GW-01)")

    r = client.get("/kpi/summary")
    check("GET /kpi/summary → 200", r.status_code == 200, f"status={r.status_code}")
    kpi = r.json()
    check("kpi.source == redis", kpi.get("source") == "redis", str(kpi.get("source")))
    check("kpi.advisory exists", "advisory" in kpi, str(list(kpi.keys())))
    check("kpi.execution exists", "execution" in kpi, "")
    check("kpi.window == 24h", kpi.get("window") == "24h", "")

    for w in ("1h", "6h", "24h", "7d"):
        r = client.get(f"/kpi/trend?window={w}")
        check(f"GET /kpi/trend?window={w} → 200", r.status_code == 200, f"status={r.status_code}")
        trend = r.json()
        check(f"trend.lanes has 4 entries (window={w})", len(trend.get("lanes", {})) == 4, str(len(trend.get("lanes", {}))))

    r = client.get("/kpi/trend?window=bad")
    check("GET /kpi/trend?window=bad → 422", r.status_code == 422, f"status={r.status_code}")

    # ── 3. Playbooks ──────────────────────────────────────────────────────────
    section("Playbooks (GW-02)")

    r = client.get("/playbooks")
    check("GET /playbooks → 200", r.status_code == 200, f"status={r.status_code}")
    pbs = r.json()
    check("playbooks.total >= 0", isinstance(pbs.get("total"), int), str(pbs.get("total")))
    check("playbooks.playbooks is list", isinstance(pbs.get("playbooks"), list), "")

    # Test get specific playbook
    r2 = client.get("/playbooks/ddos-edge-ingress-mitigation")
    check("GET /playbooks/ddos-edge-ingress-mitigation → 200", r2.status_code == 200, f"status={r2.status_code}")
    if r2.status_code == 200:
        pb = r2.json()
        check("playbook has playbook_id", "playbook_id" in pb, str(list(pb.keys())[:5]))
        check("playbook has steps", "steps" in pb, "")

    # Test 404 for unknown playbook
    r3 = client.get("/playbooks/does-not-exist-xyz")
    check("GET /playbooks/nonexistent → 404", r3.status_code == 404, f"status={r3.status_code}")

    # ── 4. SIEM Overview ──────────────────────────────────────────────────────
    section("SIEM Overview (GW-02)")

    r = client.get("/siem/overview")
    check("GET /siem/overview → 200", r.status_code == 200, f"status={r.status_code}")
    siem = r.json()
    check("siem.chain exists", "chain" in siem, str(list(siem.keys())))
    check("siem.chain.total_blocks >= 0", isinstance(siem.get("chain", {}).get("total_blocks"), int), str(siem.get("chain", {}).get("total_blocks")))
    check("siem.recent_blocks is list", isinstance(siem.get("recent_blocks"), list), "")
    check("siem.verdict_distribution_24h exists", "verdict_distribution_24h" in siem, "")

    r2 = client.get("/siem/overview?limit=5")
    check("GET /siem/overview?limit=5 → 200", r2.status_code == 200, f"status={r2.status_code}")
    siem5 = r2.json()
    check("limit=5 returns ≤5 blocks", len(siem5.get("recent_blocks", [])) <= 5, str(len(siem5.get("recent_blocks", []))))

    r3 = client.get("/siem/overview?limit=200")
    check("GET /siem/overview?limit=200 → 422 (exceeds max)", r3.status_code == 422, f"status={r3.status_code}")

    # ── 5. Agents ─────────────────────────────────────────────────────────────
    section("Agents Heartbeat (GW-02)")

    r = client.get("/agents")
    check("GET /agents → 200", r.status_code == 200, f"status={r.status_code}")
    agents = r.json()
    check("agents.overall exists", "overall" in agents, str(list(agents.keys())))
    check("agents.agents is list", isinstance(agents.get("agents"), list), "")
    check("agents.count >= 0", isinstance(agents.get("count"), int), str(agents.get("count")))

    # Check that workers wrote heartbeats (may be 0 if workers just restarted)
    n = agents.get("count", 0)
    if n > 0:
        first = agents["agents"][0]
        check("agent.role present", "role" in first, str(list(first.keys())))
        check("agent.status present", "status" in first, "")
        check("agent.age_seconds present", "age_seconds" in first, "")
        check("agent.updated_at present", "updated_at" in first, "")
    else:
        print("    INFO: no heartbeat data yet (workers may still be starting up — expected within 15s)")

    # ── 6. HITL endpoints (structure test — no real FinGuard in lab) ──────────
    section("HITL Approve/Reject (GW-03)")

    # Missing body → 422
    r = client.post("/playbooks/test-inc-001/approve", content=b"")
    check("POST /playbooks/{id}/approve no body → 422", r.status_code == 422, f"status={r.status_code}")

    # Valid body → expected 502/504 in lab (FinGuard not running), NOT 500
    r = client.post("/playbooks/test-inc-001/approve", json={"trace_id": "t-001", "reason": "lab test"})
    check("POST /playbooks/{id}/approve valid body → not 500", r.status_code != 500, f"status={r.status_code}")
    check("POST approve lab → 502 or 504 (FinGuard unreachable)", r.status_code in (502, 504), f"status={r.status_code}")

    r = client.post("/playbooks/test-inc-001/reject", json={"trace_id": "t-001", "reason": "testing"})
    check("POST /playbooks/{id}/reject valid body → not 500", r.status_code != 500, f"status={r.status_code}")
    check("POST reject lab → 502 or 504", r.status_code in (502, 504), f"status={r.status_code}")

    # ── 7. Forecast (regression check) ────────────────────────────────────────
    section("Forecast (regression check)")

    r = client.post("/forecast/matrix", json={
        "metric_name": "cpu_usage",
        "values": [0.1 * i for i in range(20)],
        "timestamps": [time.time() - (20 - i) * 300 for i in range(20)],
        "step_seconds": 300,
    })
    check("POST /forecast/matrix → 200", r.status_code == 200, f"status={r.status_code}")
    fm = r.json()
    check("forecast has 5 horizons", len(fm.get("horizons", {})) == 5, str(list(fm.get("horizons", {}).keys())))

    # ── Summary ───────────────────────────────────────────────────────────────
    total = _PASS + _FAIL
    print(f"\n{'═' * 50}")
    print(f"  TOTAL: {total}  PASS: \033[32m{_PASS}\033[0m  FAIL: \033[31m{_FAIL}\033[0m")
    print(f"  Pass rate: {100 * _PASS // total if total else 0}%")
    print(f"{'═' * 50}\n")

    # JSON report
    report_path = "/tmp/sprint2_test_results.json"
    with open(report_path, "w") as f:
        json.dump({"pass": _PASS, "fail": _FAIL, "results": _RESULTS}, f, indent=2)
    print(f"  Report: {report_path}")

    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
