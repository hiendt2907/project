#!/usr/bin/env python3
"""Enroll một Remote Agent VM qua flow IT-3 — KHÔNG sửa tay file nào trên VM.

Flow (metric sprint #3):
  1. Ensure tenant tồn tại (POST /autonomy/tenants — gotcha FK post-mortem
     drift-correction-2026-07-02: tenant PHẢI có trước mọi bảng con).
  2. Phát one-time enroll token (POST /autonomy/tenants/{tid}/enroll-tokens, admin).
  3. Đổi token lấy credential per-agent (aoip.agent.enrollment — ADR-001).
  4. Render run.env qua canonical provisioning module (không f-string tay).
  5. Push run.env lên VM qua `orb -m` + restart omni-remote-agent.service
     (bỏ qua nếu idempotent rewrite — không bounce agent vô ích).

Usage:
  .venv/bin/python scripts/enroll_remote_agent.py \
      --machine cust-app --tenant staging-sim \
      --gateway-url http://gateway.ai-agent.local \
      --admin-key "$OMNI_ADMIN_API_KEY"
"""
from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import subprocess
import sys
import time

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).parent / "lib"))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

from remote_agent_provisioning import (  # noqa: E402
    AgentProvisioningSpec,
    is_idempotent_rewrite,
    render_run_env,
)
from aoip.agent.enrollment import EnrollmentError, enroll_agent  # noqa: E402

INSTALL_DIR = "/opt/omni-remote-agent"
UNIT = "omni-remote-agent"


def _orb(machine: str, *args: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["orb", "run", "-m", machine, "-u", "root", *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _ensure_tenant(admin_url: str, admin_key: str, tenant_id: str) -> None:
    r = httpx.post(
        f"{admin_url}/autonomy/tenants",
        json={"tenant_id": tenant_id, "display_name": tenant_id, "actor": "enroll_script"},
        headers={"Authorization": f"Bearer {admin_key}"},
        timeout=15,
    )
    if r.status_code in (200, 201):
        print(f"[enroll] tenant {tenant_id!r} created")
        return
    if r.status_code in (400, 409) and "tồn tại" in r.text:
        print(f"[enroll] tenant {tenant_id!r} đã tồn tại — OK (idempotent)")
        return
    raise SystemExit(f"[enroll] ensure tenant failed: HTTP {r.status_code} {r.text[:200]}")


def _issue_enroll_token(admin_url: str, admin_key: str, tenant_id: str, label: str) -> str:
    r = httpx.post(
        f"{admin_url}/autonomy/tenants/{tenant_id}/enroll-tokens",
        json={"label": label, "actor": "enroll_script"},
        headers={"Authorization": f"Bearer {admin_key}"},
        timeout=15,
    )
    if r.status_code != 200:
        raise SystemExit(f"[enroll] issue token failed: HTTP {r.status_code} {r.text[:200]}")
    data = r.json()
    print(f"[enroll] enroll token issued (prefix={data.get('token_prefix')})")
    return data["enroll_token"]


def _build_spec(args: argparse.Namespace) -> AgentProvisioningSpec:
    return AgentProvisioningSpec(
        tenant_id=args.tenant,
        agent_id=args.agent_id,
        hostname=args.hostname,
        gateway_url=args.gateway_url,
        collect_interval=args.collect_interval,
        discovery_enabled=not args.no_discovery,
        k8s_enabled=False,
        database_enabled=args.database,
        mysql_host="127.0.0.1",
        mysql_user=args.mysql_user,
        storage_enabled=args.storage,
        doc_search_dirs=tuple(args.doc_dirs.split(",")),
        log_paths=tuple(args.log_paths.split(",")),
    )


def _push_and_restart(machine: str, run_env: str) -> None:
    existing = _orb(machine, "cat", f"{INSTALL_DIR}/run.env")
    if existing.returncode == 0 and is_idempotent_rewrite(existing.stdout, run_env):
        print("[enroll] run.env không đổi — bỏ qua write + restart (idempotent)")
        return
    w = subprocess.run(
        ["orb", "run", "-m", machine, "-u", "root", "bash", "-c",
         f"cat > {INSTALL_DIR}/run.env << 'ENVEOF'\n{run_env}\nENVEOF\n"
         f"chmod 600 {INSTALL_DIR}/run.env"],
        capture_output=True, text=True, timeout=15,
    )
    if w.returncode != 0:
        raise SystemExit(f"[enroll] write run.env failed: {w.stderr[:200]}")
    print(f"[enroll] run.env written (chmod 600) on {machine}")
    r = _orb(machine, "systemctl", "restart", UNIT, timeout=30)
    if r.returncode != 0:
        raise SystemExit(f"[enroll] restart {UNIT} failed: {r.stderr[:200]}")
    time.sleep(4)
    status = _orb(machine, "systemctl", "is-active", UNIT)
    if "active" not in status.stdout:
        raise SystemExit(f"[enroll] {UNIT} not active after restart: {status.stdout}")
    print(f"[enroll] {UNIT} active on {machine}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--machine", required=True, help="Tên VM OrbStack (orb -m <machine>)")
    p.add_argument("--tenant", required=True)
    p.add_argument("--agent-id", default=None, help="default = <machine>-agent")
    p.add_argument("--hostname", default=None, help="default = <machine>")
    p.add_argument("--gateway-url", default="http://gateway.ai-agent.local",
                   help="URL gateway mà AGENT dùng (từ trong VM)")
    p.add_argument("--admin-url", default=None,
                   help="URL gateway cho Admin API (default = --gateway-url)")
    p.add_argument("--admin-key", default=os.getenv("OMNI_ADMIN_API_KEY", ""))
    p.add_argument("--collect-interval", type=int, default=20)
    p.add_argument("--no-discovery", action="store_true")
    p.add_argument("--database", action="store_true")
    p.add_argument("--mysql-user", default="")
    p.add_argument("--storage", action="store_true")
    p.add_argument("--doc-dirs", default="/etc,/opt,/srv")
    p.add_argument("--log-paths", default="/var/log/syslog")
    p.add_argument("--dry-run", action="store_true",
                   help="Enroll + render nhưng không đụng VM (in run.env đã che key)")
    args = p.parse_args()

    if not args.admin_key:
        raise SystemExit("[enroll] cần --admin-key hoặc env OMNI_ADMIN_API_KEY")
    args.agent_id = args.agent_id or f"{args.machine}-agent"
    args.hostname = args.hostname or args.machine
    admin_url = (args.admin_url or args.gateway_url).rstrip("/")

    _ensure_tenant(admin_url, args.admin_key, args.tenant)
    token = _issue_enroll_token(admin_url, args.admin_key, args.tenant,
                                label=f"enroll:{args.machine}")

    try:
        result = asyncio.run(enroll_agent(
            admin_url, enroll_token=token,
            agent_id=args.agent_id, hostname=args.hostname,
        ))
    except EnrollmentError as exc:
        raise SystemExit(f"[enroll] {exc}")
    print(f"[enroll] credential issued: tenant={result.tenant_id} "
          f"agent={result.agent_id} key_prefix={result.key_prefix}")

    spec = _build_spec(args)
    run_env = render_run_env(spec, api_key=result.api_key)
    if args.dry_run:
        print(run_env.replace(result.api_key, f"{result.key_prefix}***"))
        return
    _push_and_restart(args.machine, run_env)
    print(f"[enroll] DONE — verify: curl {admin_url}/agents/remote | grep {args.agent_id}")


if __name__ == "__main__":
    main()
