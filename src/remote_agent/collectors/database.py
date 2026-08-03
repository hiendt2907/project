"""Remote agent collector — database health (MySQL, ProxySQL).

Probes:
  mysql_health       → domain=database  (lane=SYS_HARD_FAIL, deprecated)
  proxysql_stats     → domain=database  (lane=SYS_RESOURCE, deprecated)

Binary state (server down, replica not running) — verdict stays here.

All commands are read-only; no mutations.
Uses asyncio.create_subprocess_exec — no blocking subprocess.run().
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from remote_agent import exec_guard
from pkg.domain.taxonomy import DATABASE
from remote_agent.evidence import build_envelope

logger = logging.getLogger(__name__)

_MYSQL_CONNECT_TIMEOUT = 3
_PROXYSQL_ADMIN_PORT = 6032


async def _run(cmd: list[str], timeout: float = 8.0, env: dict[str, str] | None = None) -> tuple[str, str, int]:
    """Run a subprocess, return (stdout, stderr, returncode). Never raises.

    Pass env to inject MYSQL_PWD instead of --password= in argv (hides from /proc/cmdline).
    """
    # Cùng validator với command channel — collector KHÔNG có đường riêng.
    reason = exec_guard.check(cmd)
    if reason:
        return "", f"blocked: {reason}", 1
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode or 0
    except asyncio.TimeoutError:
        return "", "timeout", 1
    except Exception as exc:
        return "", str(exc), 1


async def collect_mysql_health(
    hostname: str,
    *,
    mysql_host: str = "127.0.0.1",
    mysql_port: int = 3306,
    mysql_user: str = "",
    mysql_pass: str = "",
) -> dict[str, Any] | None:
    """Collect MySQL global status + replication state (read-only)."""
    status_sql = (
        "SHOW STATUS WHERE Variable_name IN ('Uptime','Threads_connected','Threads_running',"
        "'Questions','Slow_queries','Com_select','Com_insert','Com_update',"
        "'Innodb_buffer_pool_read_requests','Innodb_buffer_pool_reads',"
        "'Aborted_connects','Max_used_connections');"
    )
    cmd_status = [
        "mysql",
        f"--host={mysql_host}",
        f"--port={str(mysql_port)}",
        f"--connect-timeout={_MYSQL_CONNECT_TIMEOUT}",
        "--batch", "--skip-column-names",
        "-e", status_sql,
    ]
    if mysql_user:
        cmd_status.insert(-2, f"--user={mysql_user}")
    # Pass password via MYSQL_PWD env var to avoid exposure in /proc/<pid>/cmdline
    mysql_env = {**os.environ, "MYSQL_PWD": mysql_pass} if mysql_pass else None
    out_status, err_status, rc_status = await _run(cmd_status, env=mysql_env)

    repl_sql = "SHOW REPLICA STATUS\\G"
    cmd_repl = [
        "mysql",
        f"--host={mysql_host}",
        f"--port={str(mysql_port)}",
        f"--connect-timeout={_MYSQL_CONNECT_TIMEOUT}",
        "--batch", "--skip-column-names",
        "-e", repl_sql,
    ]
    if mysql_user:
        cmd_repl.insert(-2, f"--user={mysql_user}")
    out_repl, _, _ = await _run(cmd_repl, env=mysql_env)

    if rc_status != 0:
        err_brief = err_status[:300].strip()
        logger.warning("[collector.database] mysql unreachable host=%s err=%s", mysql_host, err_brief)
        return build_envelope(
            probe="mysql_health",
            lane="SYS_HARD_FAIL",
            domain=DATABASE,
            result="FAILED",
            extracted_fact={
                "result": "FAILED",
                "db_engine": "mysql",
                "db_host": mysql_host,
                "db_port": mysql_port,
                "error": err_brief,
                "return_code": rc_status,
            },
            alert_rule="MySQLDown",
            alert_hint=f"[{hostname}] mysql FAILED {mysql_host}:{mysql_port}: {err_brief}",
            symptom_group="database_health",
            namespace=hostname,
        )

    # Parse key=value pairs from status output
    fact: dict[str, Any] = {"db_engine": "mysql", "db_host": mysql_host, "db_port": mysql_port}
    for line in out_status.splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) == 2:
            key, val = parts[0].lower(), parts[1].strip()
            try:
                fact[key] = int(val)
            except ValueError:
                fact[key] = val

    # Replication lag
    repl_lag: int | None = None
    repl_running = True
    for line in out_repl.splitlines():
        if "Seconds_Behind_Source:" in line or "Seconds_Behind_Master:" in line:
            val_str = line.split(":", 1)[-1].strip()
            if val_str.isdigit():
                repl_lag = int(val_str)
                fact["replication_lag_s"] = repl_lag
        if "Replica_SQL_Running: No" in line or "Slave_SQL_Running: No" in line:
            repl_running = False
            fact["replica_sql_running"] = False

    # Assess anomaly
    threads = int(fact.get("threads_connected", 0))
    slow = int(fact.get("slow_queries", 0))
    anomalies = []
    if not repl_running:
        anomalies.append("replication SQL thread stopped")
    if repl_lag is not None and repl_lag > 300:
        anomalies.append(f"replication_lag={repl_lag}s>300s")
    if threads > 500:
        anomalies.append(f"threads_connected={threads}>500")
    if slow > 100:
        anomalies.append(f"slow_queries={slow}>100")

    result = "FAILED" if anomalies else "PASSED"
    fact["result"] = result
    hint = f"[{hostname}] MySQL {mysql_host}:{mysql_port} — " + (", ".join(anomalies) if anomalies else f"threads={threads} repl_lag={repl_lag}s OK")

    return build_envelope(
        probe="mysql_health",
        lane="SYS_HARD_FAIL" if anomalies else "SYS_RESOURCE",
        domain=DATABASE,
        result=result,
        extracted_fact=fact,
        alert_rule="MySQLAnomaly" if anomalies else "MySQLHealthy",
        alert_hint=hint,
        symptom_group="database_state",
        namespace=hostname,
    )


async def collect_proxysql_stats(
    hostname: str,
    *,
    proxysql_host: str = "127.0.0.1",
    proxysql_admin_user: str = "radmin",
    proxysql_admin_pass: str = "",
) -> dict[str, Any] | None:
    """Collect ProxySQL admin stats — connection pool + query rules (read-only)."""
    stats_sql = (
        "SELECT Variable_Name, Variable_Value FROM stats.stats_mysql_global "
        "WHERE Variable_Name IN ('Active_Transactions','Client_Connections_connected',"
        "'Client_Connections_created','Queries_backends_bytes_recv','Servers_table_version',"
        "'MySQL_Thread_Workers','Backend_query_time_nsec') LIMIT 20;"
    )
    cmd = [
        "mysql",
        f"--host={proxysql_host}",
        f"--port={_PROXYSQL_ADMIN_PORT}",
        f"--user={proxysql_admin_user}",
        "--connect-timeout=3",
        "--batch", "--skip-column-names",
        "-e", stats_sql,
    ]
    # Pass password via MYSQL_PWD env var to avoid exposure in /proc/<pid>/cmdline
    proxysql_env = {**os.environ, "MYSQL_PWD": proxysql_admin_pass} if proxysql_admin_pass else None
    out, err, rc = await _run(cmd, env=proxysql_env)

    if rc != 0:
        err_brief = err[:300].strip()
        logger.warning("[collector.database] proxysql unreachable host=%s err=%s", proxysql_host, err_brief)
        return build_envelope(
            probe="proxysql_stats",
            lane="SYS_HARD_FAIL",
            domain=DATABASE,
            result="FAILED",
            extracted_fact={
                "result": "FAILED",
                "db_engine": "proxysql",
                "db_host": proxysql_host,
                "db_port": _PROXYSQL_ADMIN_PORT,
                "error": err_brief,
                "return_code": rc,
            },
            alert_rule="ProxySQLDown",
            alert_hint=f"[{hostname}] proxysql FAILED {proxysql_host}:{_PROXYSQL_ADMIN_PORT}: {err_brief}",
            symptom_group="database_health",
            namespace=hostname,
        )

    fact: dict[str, Any] = {"db_engine": "proxysql", "db_host": proxysql_host, "db_port": _PROXYSQL_ADMIN_PORT}
    for line in out.splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) == 2:
            key, val = parts[0].lower(), parts[1].strip()
            try:
                fact[key] = int(val)
            except ValueError:
                fact[key] = val

    active_tx = int(fact.get("active_transactions", 0))
    clients = int(fact.get("client_connections_connected", 0))

    anomalies = []
    if clients > 2000:
        anomalies.append(f"proxysql_clients={clients}>2000")
    if active_tx > 500:
        anomalies.append(f"active_transactions={active_tx}>500")

    result = "FAILED" if anomalies else "PASSED"
    fact["result"] = result
    hint = f"[{hostname}] ProxySQL {proxysql_host} — " + (", ".join(anomalies) if anomalies else f"clients={clients} active_tx={active_tx} OK")

    return build_envelope(
        probe="proxysql_stats",
        lane="SYS_HARD_FAIL" if anomalies else "SYS_RESOURCE",
        domain=DATABASE,
        result=result,
        extracted_fact=fact,
        alert_rule="ProxySQLAnomaly" if anomalies else "ProxySQLHealthy",
        alert_hint=hint,
        symptom_group="database_state",
        namespace=hostname,
    )
