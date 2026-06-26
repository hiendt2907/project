"""Tail log files and count ERROR/CRITICAL lines in the last N minutes."""
from __future__ import annotations

import asyncio
import glob
import logging
import re
from typing import Any

from remote_agent.evidence import build_envelope

logger = logging.getLogger(__name__)

_ERROR_RE = re.compile(r"\b(ERROR|CRITICAL|FATAL|Exception|Traceback)\b", re.IGNORECASE)
_WINDOW_SEC = 300  # 5 minutes
_TAIL_LINES = 500
_ERROR_THRESHOLD = 5


def _tail_lines(path: str, n: int) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, n * 200)
            f.seek(max(0, size - chunk))
            data = f.read().decode("utf-8", errors="replace")
            return data.splitlines()[-n:]
    except Exception:
        return []


async def collect_log_errors(log_paths: list[str], hostname: str) -> list[dict[str, Any]]:
    """Scan log files for recent errors and return a single aggregated envelope.

    Mirrors the aggregation pattern used by every other collector (disk_usage,
    storage_nfs, service_systemd_units, ...): one envelope per cycle regardless
    of how many files/entities are inspected, listing only the offending ones.
    A per-file envelope would multiply 1 poll cycle into N envelopes (N = number
    of glob matches) even when every file is clean.
    """
    expanded: list[str] = []
    for pattern in log_paths:
        expanded.extend(glob.glob(pattern))

    per_file: list[dict[str, Any]] = []
    failed_files: list[str] = []

    for path in expanded:
        try:
            lines = await asyncio.get_event_loop().run_in_executor(None, _tail_lines, path, _TAIL_LINES)
        except Exception as exc:
            logger.warning("[collector.logs] cannot read %s: %s", path, exc)
            continue

        errors = [l for l in lines if _ERROR_RE.search(l)]
        count = len(errors)
        if count >= _ERROR_THRESHOLD:
            failed_files.append(path)
            per_file.append({
                "log_path": path,
                "error_count": count,
                "sample": errors[-3:],
            })

    if not expanded:
        return []

    result = "FAILED" if failed_files else "PASSED"
    hint = (
        f"[{hostname}] log errors: {len(failed_files)}/{len(expanded)} files over threshold — {failed_files[:5]}"
        if failed_files
        else f"[{hostname}] log errors: {len(expanded)} files scanned, all clean"
    )

    env = build_envelope(
        probe="remote_log_errors",
        lane="APP_HTTP",
        result=result,
        extracted_fact={
            "files_scanned": len(expanded),
            "failed_files": per_file,
            "failed_file_count": len(failed_files),
            "threshold": _ERROR_THRESHOLD,
        },
        alert_rule="RemoteLogErrorSurge" if failed_files else "RemoteLogNormal",
        alert_hint=hint,
        symptom_group="app_log_error",
        namespace=hostname,
        # PASSED = clean scan → rolling log store only; FAILED = error surge → alert pipeline.
        signal_type="ANOMALY" if failed_files else "LOG_SAMPLE",
    )
    return [env]
