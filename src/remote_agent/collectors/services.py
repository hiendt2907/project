"""Remote agent collector — services health (HAProxy, systemd units).

Probes:
  service_haproxy        → DOMAIN_SERVICES  lane=SYS_HARD_FAIL / SYS_RESOURCE
  service_systemd_units  → DOMAIN_SERVICES  lane=SYS_HARD_FAIL

All commands are read-only; no mutations.
Uses asyncio.create_subprocess_exec — no blocking subprocess.run().
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from remote_agent.evidence import build_envelope

logger = logging.getLogger(__name__)

_HAPROXY_STATS_SOCKET = "/run/haproxy/admin.sock"
_HAPROXY_STATS_PORT = 9000  # prometheus-haproxy-exporter default

_CRITICAL_SERVICES = frozenset({
    "mysql", "proxysql", "haproxy", "nginx", "postgresql",
    "zabbix-server", "kafka", "redis",
})


async def _run(cmd: list[str], stdin: str | None = None, timeout: float = 8.0) -> tuple[str, str, int]:
    """Run subprocess, optionally pipe stdin. Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        in_bytes = stdin.encode() if stdin else None
        out, err = await asyncio.wait_for(proc.communicate(in_bytes), timeout=timeout)
        return out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode or 0
    except asyncio.TimeoutError:
        return "", "timeout", 1
    except Exception as exc:
        return "", str(exc), 1


async def _query_haproxy_socket(socket_path: str, command: str, timeout: float = 5.0) -> tuple[str, str, int]:
    """Query HAProxy stats socket via Python asyncio (no socat dependency)."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socket_path), timeout=timeout
        )
        try:
            writer.write(command.encode())
            await writer.drain()
            chunks: list[bytes] = []
            while True:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode(errors="replace"), "", 0
        finally:
            writer.close()
            await writer.wait_closed()
    except asyncio.TimeoutError:
        return "", "timeout", 1
    except Exception as exc:
        return "", str(exc), 1


async def collect_haproxy_stats(hostname: str) -> dict[str, Any] | None:
    """Collect HAProxy CSV stats via unix socket (read-only 'show stat')."""
    # Try Python asyncio unix socket first (no socat dependency)
    out, err, rc = await _query_haproxy_socket(_HAPROXY_STATS_SOCKET, "show stat\n")

    if rc != 0:
        logger.debug("[collector.services] haproxy socket unavailable, trying http stats: %s", err[:100])
        out, err, rc = await _run(
            ["curl", "-sf", f"http://127.0.0.1:{_HAPROXY_STATS_PORT}/metrics"],
        )
        if rc != 0:
            logger.warning("[collector.services] haproxy stats unavailable: %s", err[:200])
            return None
        return _parse_haproxy_prom_metrics(out, hostname)

    return _parse_haproxy_csv(out, hostname)


def _parse_haproxy_csv(csv_text: str, hostname: str) -> dict[str, Any]:
    """Parse HAProxy CSV stat output into fact dict."""
    lines = [l for l in csv_text.splitlines() if l and not l.startswith("#")]
    down_backends: list[str] = []
    total_sessions = 0
    total_bytes_in = 0

    for line in lines:
        cols = line.split(",")
        if len(cols) < 20:
            continue
        pxname, svname, status = cols[0], cols[1], cols[17] if len(cols) > 17 else ""
        scur = cols[4] if len(cols) > 4 else "0"
        bin_val = cols[8] if len(cols) > 8 else "0"
        try:
            total_sessions += int(scur or 0)
            total_bytes_in += int(bin_val or 0)
        except ValueError:
            pass
        if svname not in ("FRONTEND", "BACKEND") and status and "DOWN" in status.upper():
            down_backends.append(f"{pxname}/{svname}")

    fact: dict[str, Any] = {
        "service": "haproxy",
        "down_backends": down_backends,
        "down_backend_count": len(down_backends),
        "total_current_sessions": total_sessions,
        "total_bytes_in": total_bytes_in,
    }

    anomalies = []
    if down_backends:
        anomalies.append(f"backends_down={down_backends[:5]}")

    result = "FAILED" if anomalies else "PASSED"
    hint = f"[{hostname}] HAProxy — " + (", ".join(anomalies) if anomalies else f"sessions={total_sessions} all backends UP")

    return build_envelope(
        probe="service_haproxy",
        lane="SYS_HARD_FAIL" if down_backends else "SYS_RESOURCE",
        result=result,
        extracted_fact=fact,
        alert_rule="HAProxyBackendDown" if down_backends else "HAProxyHealthy",
        alert_hint=hint,
        symptom_group="service_state",
        namespace=hostname,
    )


def _parse_haproxy_prom_metrics(prom_text: str, hostname: str) -> dict[str, Any]:
    """Minimal Prometheus text format parser for HAProxy exporter."""
    down_backends: list[str] = []
    for line in prom_text.splitlines():
        if line.startswith("haproxy_server_up") and ' 0' in line:
            down_backends.append(line.split("{", 1)[-1].split("}")[0] if "{" in line else "unknown")

    fact: dict[str, Any] = {"service": "haproxy", "down_backends": down_backends, "down_backend_count": len(down_backends)}
    result = "FAILED" if down_backends else "PASSED"
    hint = f"[{hostname}] HAProxy (prom) — " + (f"backends_down={down_backends[:5]}" if down_backends else "all UP")

    return build_envelope(
        probe="service_haproxy",
        lane="SYS_HARD_FAIL" if down_backends else "SYS_RESOURCE",
        result=result,
        extracted_fact=fact,
        alert_rule="HAProxyBackendDown" if down_backends else "HAProxyHealthy",
        alert_hint=hint,
        symptom_group="service_state",
        namespace=hostname,
    )


async def collect_systemd_units(
    hostname: str,
    critical_services: frozenset[str] | None = None,
) -> dict[str, Any] | None:
    """Collect failed / degraded systemd units (read-only).

    critical_services: set of service names discovered on this VM.
    Falls back to the hardcoded _CRITICAL_SERVICES if not provided.
    """
    out, err, rc = await _run([
        "systemctl", "list-units",
        "--type=service",
        "--state=failed,activating",
        "--no-legend", "--no-pager",
        "--plain",
    ])
    if rc != 0:
        logger.warning("[collector.services] systemctl unavailable: %s", err[:200])
        return None

    # Use per-VM discovered services; fall back to hardcoded set if not provided
    known_critical = critical_services if critical_services is not None else _CRITICAL_SERVICES

    failed: list[str] = []
    critical_failed: list[str] = []
    ignored_disabled: list[str] = []

    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        unit_full = parts[0]
        unit = unit_full.rstrip(".service")
        # Migration residue guard: a unit that is BOTH disabled AND failed was
        # stopped intentionally (e.g. agent migration) — systemd keeps the
        # failed state until reset-failed. Not an incident; report separately.
        out_en, _, _ = await _run(["systemctl", "is-enabled", unit_full], timeout=5.0)
        if out_en.strip() in ("disabled", "masked"):
            ignored_disabled.append(unit)
            continue
        failed.append(unit)
        unit_lower = unit.lower()
        if any(unit_lower == c or unit_lower.startswith(c) for c in known_critical):
            critical_failed.append(unit)

    result = "FAILED" if failed else "PASSED"
    fact: dict[str, Any] = {
        "result": result,
        "failed_units": failed,
        "failed_count": len(failed),
        "critical_failed_units": critical_failed,
        "ignored_disabled_units": ignored_disabled,
    }
    hint = (
        f"[{hostname}] systemd: {len(failed)} units failed/activating"
        + (f" CRITICAL: {critical_failed}" if critical_failed else "")
        if failed else f"[{hostname}] systemd: all monitored services OK"
    )

    return build_envelope(
        probe="service_systemd_units",
        lane="SYS_HARD_FAIL" if critical_failed else ("SYS_RESOURCE" if failed else "SYS_RESOURCE"),
        result=result,
        extracted_fact=fact,
        alert_rule="SystemdCriticalFailed" if critical_failed else ("SystemdUnitsFailed" if failed else "SystemdHealthy"),
        alert_hint=hint,
        symptom_group="service_state",
        namespace=hostname,
    )
