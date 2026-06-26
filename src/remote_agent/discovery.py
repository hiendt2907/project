"""VM auto-discovery — collects service topology on agent install and every 1h.

INVARIANT INV_NO_DATA_EXFIL: Only paths/names/stats collected, never file content.
All subprocess calls are read-only and time-bounded.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
import socket
import time
from typing import Any

logger = logging.getLogger(__name__)

_SERVICE_LOG_HINTS: dict[str, list[str]] = {
    "mysql": ["/var/log/mysql/", "/var/log/mysqld.log"],
    "mysqld": ["/var/log/mysql/", "/var/log/mysqld.log"],
    "mariadb": ["/var/log/mysql/", "/var/log/mariadb/"],
    "nginx": ["/var/log/nginx/"],
    "apache2": ["/var/log/apache2/"],
    "httpd": ["/var/log/httpd/"],
    "postgresql": ["/var/log/postgresql/"],
    "redis": ["/var/log/redis/"],
    "kafka": ["/opt/kafka/logs/", "/var/log/kafka/"],
    "haproxy": ["/var/log/haproxy.log", "/var/log/haproxy/"],
    "proxysql": ["/var/log/proxysql/"],
    "zabbix-server": ["/var/log/zabbix/"],
}

_SERVICE_CONFIG_HINTS: dict[str, list[str]] = {
    "mysql": ["/etc/mysql/"],
    "mysqld": ["/etc/mysql/", "/etc/my.cnf"],
    "mariadb": ["/etc/mysql/"],
    "nginx": ["/etc/nginx/"],
    "apache2": ["/etc/apache2/"],
    "httpd": ["/etc/httpd/"],
    "postgresql": ["/etc/postgresql/"],
    "haproxy": ["/etc/haproxy/"],
    "proxysql": ["/etc/proxysql.cnf"],
    "redis": ["/etc/redis/"],
    "kafka": ["/etc/kafka/", "/opt/kafka/config/"],
    "zabbix-server": ["/etc/zabbix/"],
}


async def _run(cmd: list[str], timeout: float = 10.0) -> tuple[str, int]:
    """Run read-only subprocess. Returns (stdout, returncode). Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode(errors="replace"), proc.returncode or 0
    except asyncio.TimeoutError:
        return "", 1
    except Exception:
        return "", 1


async def _collect_running_services() -> list[dict[str, Any]]:
    out, rc = await _run([
        "systemctl", "list-units", "--type=service", "--state=running",
        "--no-legend", "--no-pager", "--plain",
    ])
    if rc != 0 or not out.strip():
        return []
    services = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if not parts:
            continue
        unit = parts[0].rstrip(".service")
        description = parts[4].strip() if len(parts) > 4 else ""
        base = unit.split("@")[0].lower()
        services.append({
            "name": unit,
            "status": "running",
            "description": description[:120],
            "log_paths": _SERVICE_LOG_HINTS.get(base, []),
            "config_paths": _SERVICE_CONFIG_HINTS.get(base, []),
        })
    return services


async def _collect_log_paths() -> list[str]:
    """Find log file paths in /var/log — never read content."""
    out, _ = await _run([
        "find", "/var/log", "-maxdepth", "3",
        "-name", "*.log", "-not", "-empty",
    ], timeout=15.0)
    return [p.strip() for p in out.splitlines() if p.strip()][:200]


async def _collect_network_listeners() -> list[dict[str, Any]]:
    out, rc = await _run(["ss", "-tlnp"], timeout=8.0)
    if rc != 0:
        out, _ = await _run(["netstat", "-tlnp"], timeout=8.0)
    listeners = []
    for line in out.splitlines():
        if "LISTEN" not in line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        port = local.rsplit(":", 1)[-1] if ":" in local else ""
        service = ""
        if len(parts) > 6:
            m = re.search(r'"([^"]+)"', parts[-1])
            if m:
                service = m.group(1)
        if port and port.isdigit():
            listeners.append({"port": int(port), "service": service})
    return listeners[:50]


async def _collect_os_info(hostname: str) -> dict[str, str]:
    info: dict[str, str] = {
        "hostname": hostname,
        "kernel": platform.release(),
        "arch": platform.machine(),
        "distro": "",
    }
    out, _ = await _run(["cat", "/etc/os-release"], timeout=5.0)
    for line in out.splitlines():
        if line.startswith("PRETTY_NAME="):
            info["distro"] = line.split("=", 1)[1].strip().strip('"')
            break
    return info


async def _collect_installed_packages() -> list[dict[str, str]]:
    """Package names + versions only — no content."""
    out, rc = await _run(["dpkg", "-l"], timeout=15.0)
    if rc == 0 and out.strip():
        pkgs = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] in ("ii", "iU"):
                pkgs.append({"name": parts[1], "version": parts[2]})
        return pkgs[:500]
    out, rc = await _run(["rpm", "-qa", "--qf", "%{NAME} %{VERSION}\\n"], timeout=15.0)
    if rc == 0 and out.strip():
        pkgs = []
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                pkgs.append({"name": parts[0], "version": parts[1].strip()})
        return pkgs[:500]
    return []


async def run_vm_discovery(agent_id: str, hostname: str) -> dict[str, Any]:
    """Full VM discovery scan. Returns VMProfile dict.

    INVARIANT INV_NO_DATA_EXFIL: no file content ever included.
    """
    logger.info("[discovery] starting VM scan agent_id=%s hostname=%s", agent_id, hostname)
    t0 = time.monotonic()

    results = await asyncio.gather(
        _collect_running_services(),
        _collect_log_paths(),
        _collect_network_listeners(),
        _collect_os_info(hostname),
        _collect_installed_packages(),
        return_exceptions=True,
    )

    services    = results[0] if not isinstance(results[0], Exception) else []
    log_paths   = results[1] if not isinstance(results[1], Exception) else []
    listeners   = results[2] if not isinstance(results[2], Exception) else []
    os_info     = results[3] if not isinstance(results[3], Exception) else {"hostname": hostname}
    packages    = results[4] if not isinstance(results[4], Exception) else []

    elapsed = round(time.monotonic() - t0, 2)
    logger.info(
        "[discovery] complete agent_id=%s services=%d log_paths=%d elapsed=%.2fs",
        agent_id, len(services), len(log_paths), elapsed,
    )

    return {
        "agent_id": agent_id,
        "hostname": hostname,
        "scanned_at": int(time.time()),
        "scan_duration_s": elapsed,
        "services": services,
        "log_paths": log_paths,
        "listeners": listeners,
        "os_info": os_info,
        "packages": packages,
    }


def derive_enabled_collectors(profile: dict[str, Any]) -> dict[str, bool]:
    """Derive which collectors to enable based on discovered services and packages."""
    service_names = {s["name"].lower() for s in profile.get("services", [])}
    packages = {p["name"].lower() for p in profile.get("packages", [])}

    def _has_service(*names: str) -> bool:
        """Match running services only — avoids false positives from client packages."""
        return any(name in item for name in names for item in service_names)

    def _has_pkg(*names: str) -> bool:
        return any(name in item for name in names for item in packages)

    return {
        # Database server: require running service, not just client package installed
        "database_enabled": _has_service("mysql", "mysqld", "mariadb"),
        # ProxySQL: service or package (proxysql has no separate client naming issue)
        "proxysql_enabled": _has_service("proxysql") or _has_pkg("proxysql2"),
        # Web/proxy services: check running services
        "services_enabled": _has_service("haproxy", "nginx", "apache", "httpd"),
        "storage_enabled": True,
        "k8s_enabled": _has_service("kubelet") or _has_pkg("kubernetes"),
    }


# ---------------------------------------------------------------------------
# Discovery snapshot — lưu trữ và diff topology để phát hiện thay đổi
# ---------------------------------------------------------------------------
_SNAPSHOT_KEY_PREFIX = "omni:knowledge:discovery_snapshot:"
_SNAPSHOT_TTL = 7 * 86400  # 7 ngày


def _snapshot_key(tenant_id: str, agent_id: str) -> str:
    return f"{_SNAPSHOT_KEY_PREFIX}{tenant_id}:{agent_id}"


async def save_discovery_snapshot(
    redis: Any,
    *,
    tenant_id: str,
    agent_id: str,
    snapshot: dict[str, Any],
) -> None:
    """Lưu snapshot topology vào Redis để so sánh lần sau."""
    if redis is None:
        return
    try:
        await redis.set(_snapshot_key(tenant_id, agent_id), json.dumps(snapshot), ex=_SNAPSHOT_TTL)
    except Exception as exc:
        logger.warning("discovery: save_snapshot failed agent=%s err=%r", agent_id, exc)


async def load_discovery_snapshot(
    redis: Any,
    *,
    tenant_id: str,
    agent_id: str,
) -> dict[str, Any] | None:
    """Load snapshot trước đó. Trả None nếu chưa có."""
    if redis is None:
        return None
    try:
        raw = await redis.get(_snapshot_key(tenant_id, agent_id))
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("discovery: load_snapshot failed agent=%s err=%r", agent_id, exc)
        return None


def diff_discovery(
    old: dict[str, Any],
    new: dict[str, Any],
) -> list[dict[str, str]]:
    """So sánh 2 snapshot topology. Trả list change records.

    Mỗi record: {change_type, entity_type, entity_name, old_value, new_value}.
    Chỉ diff services và network_listeners (thay đổi có ý nghĩa vận hành).
    """
    changes: list[dict[str, str]] = []

    old_services = {s["name"] for s in old.get("services", []) if s.get("name")}
    new_services = {s["name"] for s in new.get("services", []) if s.get("name")}

    for svc in new_services - old_services:
        changes.append({
            "change_type": "SERVICE_ADDED",
            "entity_type": "service",
            "entity_name": svc,
            "old_value": "",
            "new_value": svc,
        })
    for svc in old_services - new_services:
        changes.append({
            "change_type": "SERVICE_REMOVED",
            "entity_type": "service",
            "entity_name": svc,
            "old_value": svc,
            "new_value": "",
        })

    def _listener_set(snap: dict[str, Any]) -> set[str]:
        return {
            f"{l.get('proto','tcp')}:{l.get('port','')}"
            for l in snap.get("network_listeners", [])
            if l.get("port")
        }

    old_listeners = _listener_set(old)
    new_listeners = _listener_set(new)

    for lsn in new_listeners - old_listeners:
        changes.append({
            "change_type": "PORT_OPENED",
            "entity_type": "network_listener",
            "entity_name": lsn,
            "old_value": "",
            "new_value": lsn,
        })
    for lsn in old_listeners - new_listeners:
        changes.append({
            "change_type": "PORT_CLOSED",
            "entity_type": "network_listener",
            "entity_name": lsn,
            "old_value": lsn,
            "new_value": "",
        })

    return changes
