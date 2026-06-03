"""Collect CPU, memory, disk metrics via psutil."""
from __future__ import annotations

import logging
from typing import Any

from remote_agent.evidence import build_envelope

logger = logging.getLogger(__name__)

_CPU_WARN = 80.0
_MEM_WARN = 85.0
_DISK_WARN = 90.0


async def collect_system_metrics(hostname: str) -> dict[str, Any] | None:
    try:
        import psutil
    except ImportError:
        logger.warning("[collector.system] psutil not installed — skipping system metrics")
        return None

    try:
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        load = list(psutil.getloadavg())

        fact: dict[str, Any] = {
            "cpu_percent": round(cpu, 2),
            "mem_percent": round(mem.percent, 2),
            "mem_used_mb": round(mem.used / 1024 / 1024, 1),
            "mem_total_mb": round(mem.total / 1024 / 1024, 1),
            "disk_percent": round(disk.percent, 2),
            "disk_used_gb": round(disk.used / 1024 ** 3, 2),
            "disk_total_gb": round(disk.total / 1024 ** 3, 2),
            "load_avg_1m": round(load[0], 2),
            "load_avg_5m": round(load[1], 2),
            "load_avg_15m": round(load[2], 2),
        }

        anomaly = cpu > _CPU_WARN or mem.percent > _MEM_WARN or disk.percent > _DISK_WARN
        result = "FAILED" if anomaly else "PASSED"

        parts = []
        if cpu > _CPU_WARN:
            parts.append(f"CPU {cpu:.1f}%>{_CPU_WARN}%")
        if mem.percent > _MEM_WARN:
            parts.append(f"MEM {mem.percent:.1f}%>{_MEM_WARN}%")
        if disk.percent > _DISK_WARN:
            parts.append(f"DISK {disk.percent:.1f}%>{_DISK_WARN}%")
        hint = f"[{hostname}] " + (", ".join(parts) if parts else f"CPU={cpu:.1f}% MEM={mem.percent:.1f}% DISK={disk.percent:.1f}%")

        return build_envelope(
            probe="remote_system_metrics",
            lane="SYS_RESOURCE",
            result=result,
            extracted_fact=fact,
            alert_rule="RemoteSystemAnomaly" if anomaly else "RemoteSystemNormal",
            alert_hint=hint,
            symptom_group="workload_resource",
            namespace=hostname,
        )
    except Exception as exc:
        logger.error("[collector.system] error: %s", exc)
        return None
