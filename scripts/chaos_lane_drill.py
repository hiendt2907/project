#!/usr/bin/env python3
"""
Chaos drill runner for Omni 4-lane diagnostic system.
Injects faults per lane and verifies auto-remediation within SLO.

Usage:
  python scripts/chaos_lane_drill.py --lane all
  python scripts/chaos_lane_drill.py --lane resource
  python scripts/chaos_lane_drill.py --lane siem --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("chaos_lane_drill")

# ── Constants ────────────────────────────────────────────────────────────────

GATEWAY_URL = os.getenv("OMNI_GATEWAY_URL", "http://localhost:8000")
GATEWAY_API_KEY = os.getenv("OMNI_GATEWAY_API_KEY", "")

_SLO_SECONDS: dict[str, int] = {
    "resource": 120,
    "hardfail": 120,
    "http": 120,
    "siem": 300,
}

_POLL_INTERVAL = 5  # seconds between CRAT/advisory checks


# ── Drill result dataclass ────────────────────────────────────────────────────

@dataclass
class DrillResult:
    lane: str
    dry_run: bool
    injected_at: float = 0.0
    detected_at: float | None = None
    advisory_verdict: str = ""
    crat_written: bool = False
    action_type: str = ""
    within_slo: bool = False
    error: str = ""
    notes: list[str] = field(default_factory=list)

    def elapsed(self) -> float:
        if self.detected_at and self.injected_at:
            return round(self.detected_at - self.injected_at, 1)
        return -1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "dry_run": self.dry_run,
            "injected_at_iso": _iso(self.injected_at),
            "detected_at_iso": _iso(self.detected_at) if self.detected_at else None,
            "elapsed_seconds": self.elapsed(),
            "advisory_verdict": self.advisory_verdict,
            "crat_written": self.crat_written,
            "action_type": self.action_type,
            "within_slo": self.within_slo,
            "slo_budget_seconds": _SLO_SECONDS.get(self.lane, 120),
            "error": self.error,
            "notes": self.notes,
        }


def _iso(ts: float | None) -> str:
    if not ts:
        return ""
    import datetime
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Fault payloads ────────────────────────────────────────────────────────────

def _resource_payload(trace_id: str) -> dict:
    """Lane 1 — resource anomaly: CPU spike alert."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "ChaosDrillCPUSpike",
                    "severity": "warning",
                    "namespace": "multi-agent",
                    "pod": f"chaos-target-{trace_id[:8]}",
                    "deployment": "chaos-target",
                    "container": "chaos-target",
                    "chaos_drill": "true",
                    "chaos_lane": "resource",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] CPU spike injected for lane-1 resource test",
                    "description": (
                        "Chaos drill: container chaos-target CPU throttling 95% for 5m. "
                        "z_cpu=4.5 (3-sigma breach). This is a synthetic drill — no real workload affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "ChaosDrillCPUSpike"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def _hardfail_payload(trace_id: str) -> dict:
    """Lane 2 — hard fail: CrashLoopBackOff synthetic alert."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "ChaosDrillCrashLoop",
                    "severity": "critical",
                    "namespace": "multi-agent",
                    "pod": f"chaos-hardfail-{trace_id[:8]}-abc12",
                    "deployment": "chaos-hardfail",
                    "reason": "CrashLoopBackOff",
                    "chaos_drill": "true",
                    "chaos_lane": "hardfail",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] CrashLoopBackOff injected for lane-2 hardfail test",
                    "description": (
                        "Chaos drill: pod chaos-hardfail in CrashLoopBackOff. "
                        "Exit code 137 (OOMKilled). Restart count: 5. "
                        "This is a synthetic drill — no real workload affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "ChaosDrillCrashLoop"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def _http_payload(trace_id: str) -> dict:
    """Lane 3 — business HTTP errors: 5xx surge alert."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "ChaosDrillHTTP5xxSurge",
                    "severity": "warning",
                    "namespace": "multi-agent",
                    "pod": f"chaos-http-{trace_id[:8]}",
                    "deployment": "chaos-http",
                    "chaos_drill": "true",
                    "chaos_lane": "http",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] 5xx surge injected for lane-3 HTTP error test",
                    "description": (
                        "Chaos drill: 503 error rate 85% over 5m window (250/300 requests). "
                        "Loki access log shows sustained HTTP 503 from chaos-http service. "
                        "This is a synthetic drill — no real workload affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "ChaosDrillHTTP5xxSurge"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def _siem_payload(trace_id: str) -> dict:
    """Lane 4 — SIEM: synthetic DDoS incident alert."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "ChaosDrillSIEMDDoS",
                    "severity": "critical",
                    "namespace": "multi-agent",
                    "siem_source": "finguard",
                    "siem_category": "ddos",
                    "siem_incident_id": f"chaos-{trace_id[:12]}",
                    "source_ip": "198.51.100.1",
                    "tenant": "chaos-drill-tenant",
                    "chaos_drill": "true",
                    "chaos_lane": "siem",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] DDoS incident injected for lane-4 SIEM test",
                    "description": (
                        "Chaos drill: DDoS detected — 50,000 req/min from 198.51.100.1 "
                        "targeting multi-agent/api-gateway. Packet rate 2.5M pps. "
                        "SIEM incident_id: chaos-" + trace_id[:12] + ". "
                        "This is a synthetic drill — no real attack."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "ChaosDrillSIEMDDoS"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


_PAYLOAD_BUILDERS = {
    "resource": _resource_payload,
    "hardfail": _hardfail_payload,
    "http": _http_payload,
    "siem": _siem_payload,
}


# ── Gateway HTTP helpers ──────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if GATEWAY_API_KEY:
        h["X-API-Key"] = GATEWAY_API_KEY
    return h


async def _post_alert(client: httpx.AsyncClient, payload: dict) -> tuple[bool, str]:
    """POST payload to /webhook/prometheus. Returns (success, error)."""
    url = f"{GATEWAY_URL}/webhook/prometheus"
    try:
        resp = await client.post(url, json=payload, headers=_headers(), timeout=10.0)
        if resp.status_code < 300:
            return True, ""
        return False, f"http_{resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return False, str(exc)[:200]


async def _check_crat(client: httpx.AsyncClient, trace_id: str) -> bool:
    """Check gateway /ledger or /audit endpoint for CRAT block with this trace_id."""
    for path in ("/api/v1/audit/chain", "/audit/chain", "/ledger/blocks"):
        try:
            resp = await client.get(
                f"{GATEWAY_URL}{path}",
                headers=_headers(),
                timeout=5.0,
                params={"limit": 20},
            )
            if resp.status_code == 200:
                data = resp.json()
                blocks = data if isinstance(data, list) else data.get("blocks", [])
                for block in blocks:
                    if isinstance(block, dict):
                        if trace_id in json.dumps(block):
                            return True
        except Exception:
            pass
    return False


async def _check_advisory(client: httpx.AsyncClient, trace_id: str) -> tuple[str, str]:
    """Poll gateway for advisory verdict for this trace. Returns (verdict, action_type)."""
    # Try the agents endpoint which surfaces recent advisories
    for path in ("/api/v1/agents", "/agents"):
        try:
            resp = await client.get(
                f"{GATEWAY_URL}{path}",
                headers=_headers(),
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                agents = data if isinstance(data, list) else data.get("agents", [])
                for agent in agents:
                    meta = json.dumps(agent)
                    if trace_id[:8] in meta:
                        verdict = agent.get("last_verdict", agent.get("verdict", ""))
                        action = agent.get("last_action", agent.get("action_type", "SUGGEST_REMEDIATION"))
                        return str(verdict), str(action)
        except Exception:
            pass
    return "", ""


# ── Drill runner ──────────────────────────────────────────────────────────────

async def run_drill(lane: str, dry_run: bool) -> DrillResult:
    """Run one lane drill. Returns DrillResult."""
    import uuid

    trace_id = f"chaos-drill-{lane}-{uuid.uuid4().hex[:12]}"
    slo_budget = _SLO_SECONDS.get(lane, 120)
    result = DrillResult(lane=lane, dry_run=dry_run)

    builder = _PAYLOAD_BUILDERS.get(lane)
    if not builder:
        result.error = f"Unknown lane: {lane}"
        return result

    payload = builder(trace_id)

    if dry_run:
        result.notes.append(f"[DRY-RUN] Would POST to {GATEWAY_URL}/webhook/prometheus")
        result.notes.append(f"[DRY-RUN] trace_id={trace_id}")
        result.notes.append(f"[DRY-RUN] slo_budget={slo_budget}s")
        result.notes.append(f"[DRY-RUN] payload alertname={payload['alerts'][0]['labels']['alertname']}")
        result.within_slo = True
        return result

    result.injected_at = time.time()
    logger.info("lane=%s injecting fault trace_id=%s", lane, trace_id)

    async with httpx.AsyncClient() as client:
        ok, err = await _post_alert(client, payload)
        if not ok:
            result.error = f"injection_failed: {err}"
            result.notes.append(f"Gateway unreachable or rejected: {err}")
            return result

        result.notes.append(f"Injected at {_iso(result.injected_at)} trace_id={trace_id}")
        logger.info("lane=%s injection accepted, polling for advisory...", lane)

        # Poll for advisory detection
        deadline = result.injected_at + slo_budget
        while time.time() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)

            verdict, action = await _check_advisory(client, trace_id)
            if verdict:
                result.detected_at = time.time()
                result.advisory_verdict = verdict
                result.action_type = action
                elapsed = result.detected_at - result.injected_at
                result.within_slo = elapsed <= slo_budget
                result.notes.append(
                    f"Advisory detected after {elapsed:.1f}s: verdict={verdict} action={action}"
                )
                break

            crat_ok = await _check_crat(client, trace_id)
            if crat_ok:
                result.crat_written = True
                result.notes.append("CRAT block found in audit chain")

        if not result.advisory_verdict:
            result.error = f"No advisory detected within {slo_budget}s SLO budget"
            result.notes.append("Timeout: pipeline may be down or trace not propagated")

        # Final CRAT check
        if not result.crat_written:
            result.crat_written = await _check_crat(client, trace_id)

    return result


# ── Entry point ───────────────────────────────────────────────────────────────

ALL_LANES = ["resource", "hardfail", "http", "siem"]


async def main_async(lanes: list[str], dry_run: bool) -> int:
    results: list[DrillResult] = []

    for lane in lanes:
        print(f"\n{'='*60}")
        print(f"[CHAOS DRILL] lane={lane} dry_run={dry_run}")
        print(f"  Gateway: {GATEWAY_URL}")
        print(f"  SLO budget: {_SLO_SECONDS.get(lane, 120)}s")
        print(f"{'='*60}")

        result = await run_drill(lane, dry_run)
        results.append(result)

        if dry_run:
            print(f"  [DRY-RUN] No injection performed.")
            for note in result.notes:
                print(f"  {note}")
        else:
            status = "PASS" if result.within_slo and not result.error else "FAIL"
            print(f"  Status:         {status}")
            print(f"  Verdict:        {result.advisory_verdict or '(none)'}")
            print(f"  CRAT written:   {result.crat_written}")
            print(f"  Action type:    {result.action_type or '(none)'}")
            print(f"  Elapsed:        {result.elapsed()}s")
            print(f"  Within SLO:     {result.within_slo}")
            if result.error:
                print(f"  Error:          {result.error}")
            for note in result.notes:
                print(f"  Note: {note}")

    # Save results
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = Path(f"chaos_drill_results_{ts}.json")
    summary = {
        "timestamp": _iso(time.time()),
        "dry_run": dry_run,
        "gateway_url": GATEWAY_URL,
        "lanes_tested": lanes,
        "results": [r.to_dict() for r in results],
        "overall_pass": all(
            r.within_slo and not r.error for r in results
        ),
    }
    if not dry_run:
        out_path.write_text(json.dumps(summary, indent=2))
        print(f"\nResults saved: {out_path}")

    # Human-readable summary
    print(f"\n{'='*60}")
    print("CHAOS DRILL SUMMARY")
    print(f"{'='*60}")
    all_pass = True
    for r in results:
        ok = r.within_slo and not r.error
        flag = "PASS" if ok else "FAIL"
        print(f"  [{flag}] lane={r.lane} elapsed={r.elapsed()}s verdict={r.advisory_verdict or 'N/A'}")
        if not ok:
            all_pass = False

    print(f"\nOverall: {'PASS' if all_pass else 'FAIL'}")
    return 0 if all_pass else 1


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Chaos drill runner for Omni 4-lane diagnostic system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--lane",
        choices=["all", "resource", "hardfail", "http", "siem"],
        default="all",
        help="Lane to drill (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be injected without actually doing it",
    )
    args = parser.parse_args()

    lanes = ALL_LANES if args.lane == "all" else [args.lane]
    return asyncio.run(main_async(lanes, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
