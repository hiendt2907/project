"""Diagnostic tools — services domain (HAProxy, systemd).

Tools (read-only, no mutations):
  tool_haproxy_stats        — HAProxy backend pool + session stats
  tool_systemd_service_health — Systemd unit state check

All async; registered in tools.py; all go into READ_ONLY_FAST_PATH_TOOLS.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_HAPROXY_STATS_SOCKET = "/run/haproxy/admin.sock"
_HAPROXY_STATS_PORT = 9000


async def _run_cmd(cmd: list[str], stdin: str | None = None, timeout: float = 10.0) -> tuple[str, str, int]:
    """Run subprocess, optionally pipe stdin. Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        in_b = stdin.encode() if stdin else None
        out, err = await asyncio.wait_for(proc.communicate(in_b), timeout=timeout)
        return out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode or 0
    except asyncio.TimeoutError:
        return "", "timeout", 1
    except Exception as exc:
        return "", str(exc), 1


async def tool_haproxy_stats(ctx: Any, args: dict[str, Any]) -> str:
    """
    Fetch HAProxy stats via unix socket 'show stat' command (read-only).
    Falls back to HTTP stats endpoint if socket unavailable.

    args:
      socket       — Unix socket path (default: /run/haproxy/admin.sock)
      stats_port   — HTTP stats port fallback (default: 9000)
      show_info    — Also run 'show info' (default: false)
    """
    socket_path = str(args.get("socket") or _HAPROXY_STATS_SOCKET)
    stats_port = int(args.get("stats_port") or _HAPROXY_STATS_PORT)
    show_info = bool(args.get("show_info", False))

    lines = ["=== haproxy_stats ==="]

    # Try socket
    out_stat, err_stat, rc_stat = await _run_cmd(
        ["socat", "-", f"UNIX-CONNECT:{socket_path}"],
        stdin="show stat\n",
    )

    if rc_stat == 0 and out_stat.strip():
        lines.append(f"source: socket {socket_path}")
        lines.append("=== show stat (CSV) ===")
        # Show header + first 30 lines
        csv_lines = [l for l in out_stat.splitlines() if l]
        lines.extend(csv_lines[:30])
        if len(csv_lines) > 30:
            lines.append(f"... ({len(csv_lines) - 30} more rows)")

        if show_info:
            out_info, _, _ = await _run_cmd(
                ["socat", "-", f"UNIX-CONNECT:{socket_path}"],
                stdin="show info\n",
            )
            if out_info.strip():
                lines.append("=== show info ===")
                lines.append(out_info[:2000])
    else:
        lines.append(f"socket unavailable ({err_stat[:80]}), trying HTTP stats port={stats_port}")
        out_http, err_http, rc_http = await _run_cmd(
            ["curl", "-sf", "--max-time", "5", f"http://127.0.0.1:{stats_port}/metrics"],
        )
        if rc_http == 0:
            lines.append("source: http metrics endpoint")
            # Filter to interesting lines
            relevant = [l for l in out_http.splitlines() if l and not l.startswith("#") and "haproxy_server" in l][:40]
            lines.extend(relevant)
            if not relevant:
                lines.append(out_http[:3000])
        else:
            lines.append(f"ERROR: HAProxy stats not accessible — {err_http[:200]}")

    return "\n".join(lines)


async def tool_systemd_service_health(ctx: Any, args: dict[str, Any]) -> str:
    """
    Check systemd service states for a list of target services (read-only).
    Returns: service name, load/active/sub state, and any recent journal errors.

    args:
      services     — list of service names to check (without .service suffix)
                     default: ["haproxy", "proxysql", "mysql", "nginx"]
      journal_lines — lines of journal to fetch per failed service (default: 20)
    """
    services_raw = args.get("services") or ["haproxy", "proxysql", "mysql", "nginx"]
    if isinstance(services_raw, str):
        services_raw = [s.strip() for s in services_raw.split(",") if s.strip()]
    journal_lines = int(args.get("journal_lines") or 20)

    lines = ["=== systemd_service_health ==="]

    for svc in services_raw[:20]:
        svc_name = svc if svc.endswith(".service") else f"{svc}.service"
        out, err, rc = await _run_cmd([
            "systemctl", "show", svc_name,
            "--property=LoadState,ActiveState,SubState,ExecMainPID,MainPID,Result,ActiveEnterTimestamp",
            "--no-pager",
        ])
        if rc != 0:
            lines.append(f"{svc_name}: ERROR {err[:80]}")
            continue

        props = {}
        for prop_line in out.splitlines():
            if "=" in prop_line:
                k, v = prop_line.split("=", 1)
                props[k] = v

        active = props.get("ActiveState", "unknown")
        sub = props.get("SubState", "unknown")
        result = props.get("Result", "")
        ts = props.get("ActiveEnterTimestamp", "")
        lines.append(f"{svc_name}: active={active} sub={sub} result={result} since={ts}")

        if active != "active":
            # Fetch recent journal for failed service
            out_j, _, _ = await _run_cmd([
                "journalctl", "-u", svc_name,
                f"--lines={journal_lines}",
                "--no-pager", "--no-hostname",
            ], timeout=5.0)
            if out_j.strip():
                lines.append(f"  journal ({svc_name}):")
                for jl in out_j.splitlines()[-journal_lines:]:
                    lines.append(f"    {jl}")

    return "\n".join(lines)
