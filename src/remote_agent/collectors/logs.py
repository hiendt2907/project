"""Tail log files and count ERROR/CRITICAL lines in the last N minutes."""
from __future__ import annotations

import asyncio
import glob
import logging
import re
import time
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
    """Scan log files for recent errors and return evidence envelopes."""
    results: list[dict[str, Any]] = []
    now = time.time()

    expanded: list[str] = []
    for pattern in log_paths:
        expanded.extend(glob.glob(pattern))

    for path in expanded:
        try:
            lines = await asyncio.get_event_loop().run_in_executor(None, _tail_lines, path, _TAIL_LINES)
        except Exception as exc:
            logger.warning("[collector.logs] cannot read %s: %s", path, exc)
            continue

        errors = [l for l in lines if _ERROR_RE.search(l)]
        count = len(errors)
        sample = errors[-3:] if errors else []

        result = "FAILED" if count >= _ERROR_THRESHOLD else "PASSED"
        hint = f"[{hostname}] {path}: {count} error lines in last ~{_TAIL_LINES} lines"

        env = build_envelope(
            probe="remote_log_errors",
            lane="APP_HTTP",
            result=result,
            extracted_fact={
                "log_path": path,
                "error_count": count,
                "sample": sample,
                "threshold": _ERROR_THRESHOLD,
            },
            alert_rule="RemoteLogErrorSurge" if count >= _ERROR_THRESHOLD else "RemoteLogNormal",
            alert_hint=hint,
            symptom_group="app_log_error",
            namespace=hostname,
        )
        results.append(env)

    return results
