"""Diagnostic tools — database domain (MySQL, ProxySQL).

Tools (read-only, no mutations):
  tool_mysql_health       — MySQL global status + replication
  tool_proxysql_stats     — ProxySQL admin query routing stats

All async; registered in tools.py; all go into READ_ONLY_FAST_PATH_TOOLS.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_MYSQL_CONNECT_TIMEOUT = 5
_PROXYSQL_ADMIN_PORT = 6032
_PROXYSQL_ADMIN_USER = "radmin"


async def _run_cmd(cmd: list[str], timeout: float = 10.0) -> tuple[str, str, int]:
    """Run subprocess. Never raises; returns (stdout, stderr, rc)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode or 0
    except asyncio.TimeoutError:
        return "", "timeout after {:.0f}s".format(timeout), 1
    except Exception as exc:
        return "", str(exc), 1


async def tool_mysql_health(ctx: Any, args: dict[str, Any]) -> str:
    """
    Query MySQL global status + SHOW REPLICA STATUS via localhost (read-only).

    args:
      host      — MySQL host (default: 127.0.0.1)
      port      — MySQL port (default: 3306)
      user      — MySQL user (default: from env/defaults)
      max_rows  — Max rows returned from status (default: 30)
    """
    host = str(args.get("host") or "127.0.0.1")
    port = int(args.get("port") or 3306)
    user_flag = f"--user={args['user']}" if args.get("user") else ""
    max_rows = int(args.get("max_rows") or 30)

    status_sql = (
        "SELECT variable_name AS k, variable_value AS v "
        "FROM information_schema.global_status "
        "WHERE variable_name IN ("
        "'Uptime','Threads_connected','Threads_running','Questions','Slow_queries',"
        "'Com_select','Com_insert','Com_update','Com_delete',"
        "'Innodb_buffer_pool_read_requests','Innodb_buffer_pool_reads',"
        "'Aborted_connects','Max_used_connections','Connection_errors_max_connections'"
        ") ORDER BY variable_name LIMIT {max_rows};".format(max_rows=max_rows)
    )

    base_cmd = [
        "mysql",
        f"--host={host}",
        f"--port={port}",
        f"--connect-timeout={_MYSQL_CONNECT_TIMEOUT}",
        "--batch", "--table",
    ]
    if user_flag:
        base_cmd.append(user_flag)

    out_status, err_status, rc = await _run_cmd(base_cmd + ["-e", status_sql])
    if rc != 0:
        return f"[mysql_health] ERROR: could not connect to {host}:{port} — {err_status[:300]}"

    lines = ["=== mysql_health ===", f"host={host}:{port}", out_status[:3000]]

    # Replication status
    out_repl, _, _ = await _run_cmd(base_cmd + ["-e", "SHOW REPLICA STATUS\\G"])
    if not out_repl.strip():
        out_repl, _, _ = await _run_cmd(base_cmd + ["-e", "SHOW SLAVE STATUS\\G"])
    if out_repl.strip():
        lines.append("=== replication ===")
        relevant = [
            l for l in out_repl.splitlines()
            if any(k in l for k in ("Seconds_Behind", "Running", "Error", "Lag", "Position", "Source_Host"))
        ]
        lines.extend(relevant[:20])
    else:
        lines.append("replication: not configured (primary or standalone)")

    return "\n".join(lines)


async def tool_proxysql_stats(ctx: Any, args: dict[str, Any]) -> str:
    """
    Query ProxySQL admin interface stats (read-only).
    Fetches: stats_mysql_global, stats_mysql_connection_pool, runtime_mysql_servers.

    args:
      host         — ProxySQL host (default: 127.0.0.1)
      admin_port   — ProxySQL admin port (default: 6032)
      admin_user   — ProxySQL admin user (default: radmin)
    """
    host = str(args.get("host") or "127.0.0.1")
    port = int(args.get("admin_port") or _PROXYSQL_ADMIN_PORT)
    user = str(args.get("admin_user") or _PROXYSQL_ADMIN_USER)

    base_cmd = [
        "mysql",
        f"--host={host}",
        f"--port={port}",
        f"--user={user}",
        "--connect-timeout=3",
        "--batch", "--table",
    ]

    sections = [
        ("global_stats", "SELECT Variable_Name, Variable_Value FROM stats.stats_mysql_global ORDER BY Variable_Name;"),
        ("connection_pool", "SELECT hostgroup, srv_host, srv_port, status, ConnUsed, ConnFree, ConnOK, ConnERR, Queries FROM stats.stats_mysql_connection_pool ORDER BY hostgroup, srv_host;"),
        ("runtime_servers", "SELECT hostgroup_id, hostname, port, status, weight FROM runtime_mysql_servers ORDER BY hostgroup_id, hostname;"),
    ]

    lines = ["=== proxysql_stats ===", f"admin={host}:{port}"]

    for section_name, sql in sections:
        out, err, rc = await _run_cmd(base_cmd + ["-e", sql])
        if rc != 0:
            lines.append(f"[{section_name}] ERROR: {err[:200]}")
        else:
            lines.append(f"=== {section_name} ===")
            lines.append(out[:2000])

    return "\n".join(lines)


async def tool_database_replication_lag(ctx: Any, args: dict[str, Any]) -> str:
    """
    Check replication lag across MySQL replicas (read-only).
    Returns lag in seconds and I/O + SQL thread state.

    args:
      hosts  — list of MySQL host:port strings (default: ["127.0.0.1:3306"])
    """
    hosts_raw = args.get("hosts") or ["127.0.0.1:3306"]
    if isinstance(hosts_raw, str):
        hosts_raw = [hosts_raw]

    results: list[str] = ["=== database_replication_lag ==="]

    for hp in hosts_raw[:10]:
        parts = str(hp).split(":", 1)
        host, port = parts[0], int(parts[1]) if len(parts) > 1 else 3306

        cmd = [
            "mysql",
            f"--host={host}",
            f"--port={port}",
            f"--connect-timeout={_MYSQL_CONNECT_TIMEOUT}",
            "--batch", "--skip-column-names",
            "-e", "SHOW REPLICA STATUS\\G",
        ]
        out, err, rc = await _run_cmd(cmd)
        if not out.strip():
            # Try legacy SHOW SLAVE STATUS
            cmd[-1] = "SHOW SLAVE STATUS\\G"
            out, err, rc = await _run_cmd(cmd)

        if rc != 0:
            results.append(f"{host}:{port} → ERROR: {err[:150]}")
            continue

        if not out.strip():
            results.append(f"{host}:{port} → primary (no replication configured)")
            continue

        lag_line = next((l for l in out.splitlines() if "Seconds_Behind" in l), None)
        io_line = next((l for l in out.splitlines() if "IO_Running" in l or "Io_Running" in l), "")
        sql_line = next((l for l in out.splitlines() if "SQL_Running" in l), "")
        err_line = next((l for l in out.splitlines() if "Last_Error:" in l and len(l.split(":")[-1].strip()) > 1), "")

        lag_val = lag_line.split(":", 1)[-1].strip() if lag_line else "?"
        results.append(
            f"{host}:{port} → lag={lag_val}s {io_line.strip()} {sql_line.strip()}"
            + (f" ERR: {err_line.strip()[:80]}" if err_line else "")
        )

    return "\n".join(results)
