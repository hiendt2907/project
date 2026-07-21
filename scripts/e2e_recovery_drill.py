#!/usr/bin/env python3
"""Phase 1 E2E — real recovery drill, no hand-authored JSON.

Proves the durable recovery pipeline end-to-end through the REAL operator
CLI (aoip.console.approve_systemd_restart), against the real OrbStack lab
VM fleet — not a hand-built payload (that was Phase 0's proof; this script
is what makes it repeatable and scriptable). Non-destructive: the only
mutation is restarting a service explicitly labeled "(simulated)" in the
lab (payment-api.service on cust-app), which this script itself stops first
so there's something real to recover.

Sequence: stop payment-api.service -> author + enqueue via the real CLI ->
poll for terminal -> assert recovered+verified -> confirm HTTP 200 on the
service -> confirm a matching RECOVERY_COMPLETED entry landed in the VM's
real audit log.

The master kill-switch (OMNI_AUTO_EXECUTE_ENABLED) is opened on
omni-gateway ONLY for the verification window this script needs, and is
guaranteed reverted (try/finally) even on failure or Ctrl-C.

Usage:
    .venv/bin/python scripts/e2e_recovery_drill.py
    PYTHONPATH=src .venv/bin/python scripts/e2e_recovery_drill.py --runs 2
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
NS = "multi-agent"
GATEWAY = "http://localhost:18099"
VM = "cust-app"
AGENT_ID = "staging-sim_cust-app"
TENANT = "staging-sim"
UNIT = "payment-api.service"
PORT = 8080
WAIT_S = 90


def kubectl(*args: str, timeout: int = 20) -> str:
    return subprocess.run(["kubectl", *args], check=True, capture_output=True,
                          text=True, timeout=timeout).stdout.strip()


def secret(name: str, key: str) -> str:
    encoded = kubectl("get", "secret", name, "-n", NS, "-o", f"jsonpath={{.data.{key}}}")
    return base64.b64decode(encoded).decode().strip()


def orb(*args: str, timeout: int = 20) -> str:
    return subprocess.run(["orb", "-m", VM, *args], check=True, capture_output=True,
                          text=True, timeout=timeout).stdout.strip()


def orb_ok(*args: str, timeout: int = 20) -> bool:
    return subprocess.run(["orb", "-m", VM, *args], capture_output=True,
                          text=True, timeout=timeout).returncode == 0


def http(method: str, path: str, *, key: str = "", body: dict[str, Any] | bytes | None = None,
         expected: set[int] = {200}) -> tuple[int, dict[str, Any] | str]:
    if isinstance(body, dict):
        data = json.dumps(body).encode()
    else:
        data = body
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = request.Request(f"{GATEWAY}{path}", data=data, method=method, headers=headers)
    try:
        with request.urlopen(req, timeout=15) as response:
            raw = response.read().decode()
            status = response.status
    except error.HTTPError as exc:
        raw = exc.read().decode()
        status = exc.code
    try:
        parsed: dict[str, Any] | str = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw
    if status not in expected:
        raise AssertionError(f"{method} {path}: HTTP {status}, body={parsed}")
    return status, parsed


def wait_for(label: str, fn, timeout: int = WAIT_S):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(2)
    raise TimeoutError(f"timeout waiting for {label}; last={last}")


class PortForward:
    """Manages its own `kubectl port-forward` — restarts it after any gateway
    rollout (set_kill_switch triggers one), since an existing tunnel does not
    survive the pod it was connected to being replaced."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.stop()
        self._proc = subprocess.Popen(
            ["kubectl", "port-forward", "-n", NS, "svc/omni-gateway", "18099:80"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        wait_for("port-forward ready", self._is_up, timeout=20)

    def _is_up(self) -> bool:
        try:
            http("GET", "/healthz")
            return True
        except Exception:
            return False

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None


def set_kill_switch(enabled: bool) -> None:
    value = "true" if enabled else "false"
    kubectl("set", "env", "deployment/omni-gateway", "-n", NS,
           f"OMNI_AUTO_EXECUTE_ENABLED={value}")
    kubectl("rollout", "status", "deployment/omni-gateway", "-n", NS, "--timeout=60s", timeout=70)


def run_one_drill(admin_key: str, run_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {"run_id": run_id}

    orb("sudo", "systemctl", "stop", UNIT)
    result["pre_stop_active"] = orb_ok("sudo", "systemctl", "is-active", UNIT)  # expect False

    mission_id, decision_id, incident_id = f"mis-{run_id}", f"dec-{run_id}", f"inc-{run_id}"
    cli_env = {**os.environ, "PYTHONPATH": "src"}
    cli = subprocess.run(
        [sys.executable, "-m", "aoip.console.approve_systemd_restart",
         "--unit", UNIT, "--tenant", TENANT, "--agent-id", AGENT_ID,
         "--approver", f"e2e-drill-{run_id}", "--mission-id", mission_id,
         "--decision-id", decision_id, "--incident-id", incident_id,
         "--summary", f"e2e_recovery_drill.py run {run_id}",
         "--ttl-s", "600", "--diagnosis-confidence", "0.9"],
        cwd=ROOT, env=cli_env,
        capture_output=True, text=True, timeout=20,
    )
    if cli.returncode != 0:
        raise RuntimeError(f"CLI failed: {cli.stderr}")
    envelope = json.loads(cli.stdout)
    command_id = envelope["command_id"]
    result["command_id"] = command_id

    status, body = http("POST", "/webhook/agent/rt/commands/enqueue", key=admin_key, body=envelope)
    if body.get("state") != "QUEUED":  # type: ignore[union-attr]
        raise AssertionError(f"enqueue did not queue: {body}")

    def _terminal():
        _, rec = http("GET", f"/webhook/agent/rt/commands/record/{TENANT}/{command_id}", key=admin_key)
        return rec if isinstance(rec, dict) and rec.get("state") in {
            "COMPLETED", "FAILED", "ESCALATED", "EXPIRED"} else None

    record_body = wait_for("terminal state", _terminal)
    result["state"] = record_body["state"]
    result["outcome"] = record_body["outcome"]

    if record_body["state"] != "COMPLETED":
        raise AssertionError(f"expected COMPLETED, got {record_body['state']}: {record_body['outcome']}")
    if record_body["outcome"].get("status") != "recovered":
        raise AssertionError(f"expected status=recovered, got {record_body['outcome']}")
    if not record_body["outcome"].get("verified"):
        raise AssertionError(f"expected verified=true, got {record_body['outcome']}")

    http_check = subprocess.run(
        ["orb", "-m", VM, "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         f"localhost:{PORT}"], capture_output=True, text=True, timeout=15,
    )
    result["service_http_code"] = http_check.stdout.strip()
    if http_check.stdout.strip() != "200":
        raise AssertionError(f"service HTTP check failed: {http_check.stdout!r}")

    audit_tail = orb("sudo", "tail", "-n", "20", "/var/lib/aoip/recovery-audit.jsonl")
    audit_lines = [json.loads(line) for line in audit_tail.splitlines() if line.strip()]
    matching = [
        e for e in audit_lines
        if e.get("event_type") == "RECOVERY_COMPLETED"
        and e.get("payload", {}).get("action_id", "").endswith(f"dec-{run_id}-{UNIT}")
    ]
    result["audit_block_found"] = bool(matching)
    if not matching:
        raise AssertionError(
            f"no RECOVERY_COMPLETED audit entry found for action_id ending "
            f"dec-{run_id}-{UNIT} in last 20 audit lines"
        )

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1, help="consecutive drill runs (Exit Criteria wants 2)")
    args = parser.parse_args()

    admin_key = secret("omni-gateway-secret", "OMNI_ADMIN_API_KEYS")

    pf = PortForward()
    pf.start()

    passed = 0
    failed = 0
    set_kill_switch(True)
    pf.start()  # rollout from set_kill_switch replaces the pod; reconnect
    try:
        for i in range(args.runs):
            run_id = uuid.uuid4().hex[:8]
            print(f"--- run {i + 1}/{args.runs} (run_id={run_id}) ---")
            try:
                result = run_one_drill(admin_key, run_id)
                print(f"[PASS] {json.dumps(result, indent=2)}")
                passed += 1
            except Exception as exc:
                print(f"[FAIL] run_id={run_id}: {exc}")
                failed += 1
    finally:
        set_kill_switch(False)
        pf.start()  # reconnect again post-rollout to verify teardown for real
        _, mutation_state = http("GET", f"/autonomy/mutation?tenant_id={TENANT}", key=admin_key)
        print(f"[TEARDOWN] kill-switch reverted, effective mutation state: {mutation_state}")
        pf.stop()

    print(f"=== {passed} passed, {failed} failed (of {args.runs}) ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
