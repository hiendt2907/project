"""Collect CPU, memory, disk metrics via psutil."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from pkg.domain.taxonomy import OS_HOST
from remote_agent.evidence import build_envelope

logger = logging.getLogger(__name__)

# Static fences, kept only to be REPORTED to Omni (``thresholds_seen``) — the
# agent no longer compares against them to decide "anomaly". Deciding what is
# abnormal is Omni's job: it holds the per-host baseline and the confidence
# level. A static number on the customer host cannot know either.
# Overridden at runtime by thresholds pushed from Omni via the
# /webhook/agent/register response (see agent.py).
_CPU_WARN = 80.0
_MEM_WARN = 85.0
_DISK_WARN = 90.0


async def collect_system_metrics(
    hostname: str,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    try:
        import psutil
    except ImportError:
        logger.warning("[collector.system] psutil not installed — skipping system metrics")
        return None

    t = thresholds or {}
    cpu_warn = float(t.get("cpu_warn", _CPU_WARN))
    mem_warn = float(t.get("mem_warn", _MEM_WARN))
    disk_warn = float(t.get("disk_warn", _DISK_WARN))

    try:
        # interval=1 nội bộ psutil làm time.sleep(1) đồng bộ — gọi thẳng trong
        # async def sẽ đóng băng CẢ event loop của agent 1s/chu kỳ, kể cả vòng poll
        # command-channel (agent.py, _CMD_POLL_INTERVAL=5) đang chạy chung loop này.
        # run_in_executor đẩy phần block sang thread pool, event loop vẫn sống
        # (audit 2026-08-10, docs/audit/BACKEND_AUDIT_PLAN_2026-08-10.md #5).
        cpu = await asyncio.get_running_loop().run_in_executor(
            None, psutil.cpu_percent, 1
        )
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
            # The static fence is REPORTED, not applied: Omni needs to know what
            # hedge the host was configured with, but the verdict is Omni's.
            "thresholds_seen": {
                "cpu_warn": cpu_warn,
                "mem_warn": mem_warn,
                "disk_warn": disk_warn,
            },
        }

        hint = (
            f"[{hostname}] CPU={cpu:.1f}% MEM={mem.percent:.1f}% "
            f"DISK={disk.percent:.1f}%"
        )

        return build_envelope(
            probe="remote_system_metrics",
            lane="SYS_RESOURCE",
            domain=OS_HOST,
            # No verdict here. This collector reads NUMBERS; whether a number is
            # abnormal depends on the host's baseline and Omni's confidence
            # level, neither of which lives on the customer machine.
            result="OBSERVED",
            extracted_fact=fact,
            alert_rule="RemoteSystemSample",
            alert_hint=hint,
            symptom_group="workload_resource",
            namespace=hostname,
            # Always a sample: Omni's knowledge pipeline decides on deviation.
            signal_type="METRIC_SAMPLE",
        )
    except Exception as exc:
        logger.error("[collector.system] error: %s", exc)
        return None
