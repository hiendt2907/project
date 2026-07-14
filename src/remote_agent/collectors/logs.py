"""Tail log files and count ERROR/CRITICAL lines in the last N minutes."""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import re
from collections import Counter
from typing import Any

from remote_agent.evidence import build_envelope

logger = logging.getLogger(__name__)

_ERROR_RE = re.compile(r"\b(ERROR|CRITICAL|FATAL|Exception|Traceback)\b", re.IGNORECASE)
_WINDOW_SEC = 300  # 5 minutes
_TAIL_LINES = 500
_ERROR_THRESHOLD = 5
_ACCESS_TAIL_LINES = 800
_ACCESS_MAX_RECORDS = 100
_HTTP_METHODS = "GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE"
_ACCESS_RE = re.compile(
    rf'"(?P<method>{_HTTP_METHODS})\s+(?P<target>\S+)(?:\s+HTTP/[^\"]+)?"\s+(?P<status>\d{{3}})'
)
_ROUTE_SEGMENT_RE = re.compile(r"^[0-9a-f]{8,}$|^\d+$", re.IGNORECASE)
_UPSTREAM_RE = re.compile(r"(?:upstream(?:_addr|_host)?|backend)[=: ]+(?P<upstream>[A-Za-z0-9_.:-]+)", re.IGNORECASE)


def normalize_api_route(target: str) -> str:
    """Return a bounded route shape, never a query string or credential value."""
    path = target.split("?", 1)[0].split("#", 1)[0] or "/"
    path = path[:160]
    segments = [":id" if _ROUTE_SEGMENT_RE.match(segment) else segment[:64] for segment in path.split("/")]
    return "/".join(segments) or "/"


def _parse_access_line(line: str) -> dict[str, Any] | None:
    """Parse metadata from common/JSON access logs; raw lines never leave host."""
    stripped = line.strip()
    if stripped.startswith("{"):
        try:
            item = json.loads(stripped)
        except (TypeError, json.JSONDecodeError):
            item = None
        if isinstance(item, dict):
            method = str(item.get("method") or item.get("http_method") or item.get("request_method") or "").upper()
            target = str(item.get("route") or item.get("path") or item.get("url") or item.get("request_uri") or "/")
            status = item.get("status") or item.get("status_code") or item.get("http_status")
            upstream = item.get("upstream") or item.get("upstream_addr") or item.get("backend") or ""
            try:
                status_int = int(status)
            except (TypeError, ValueError):
                status_int = 0
            if method in _HTTP_METHODS.split("|") and 100 <= status_int <= 599:
                return {"method": method, "route": normalize_api_route(target), "status_class": f"{status_int // 100}xx", "upstream": str(upstream)[:120]}
    match = _ACCESS_RE.search(stripped)
    if not match:
        return None
    upstream_match = _UPSTREAM_RE.search(stripped)
    return {
        "method": match.group("method").upper(),
        "route": normalize_api_route(match.group("target")),
        "status_class": f"{int(match.group('status')) // 100}xx",
        "upstream": upstream_match.group("upstream")[:120] if upstream_match else "",
    }


def parse_api_access_lines(lines: list[str], source_path: str) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str, str, str]] = Counter()
    for line in lines:
        record = _parse_access_line(line)
        if record:
            counts[(record["method"], record["route"], record["status_class"], record["upstream"])] += 1
    return [
        {"method": method, "route": route, "status_class": status_class, "upstream": upstream,
         "count": count, "source_path": source_path[:200]}
        for (method, route, status_class, upstream), count in counts.most_common(_ACCESS_MAX_RECORDS)
    ]


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


async def collect_api_access(log_paths: list[str], hostname: str) -> dict[str, Any] | None:
    """Collect route/method/status shapes from access logs for topology sequencing.

    This is metadata-only: query strings, headers, bodies, tokens and raw log lines
    are intentionally excluded. A missing/unsupported log is represented by no
    envelope, so callers can distinguish network-only evidence from HTTP evidence.
    """
    interactions: list[dict[str, Any]] = []
    scanned: list[str] = []
    for path in sorted(set(p for pattern in log_paths for p in glob.glob(pattern))):
        lines = await asyncio.get_event_loop().run_in_executor(None, _tail_lines, path, _ACCESS_TAIL_LINES)
        records = parse_api_access_lines(lines, path)
        if records:
            scanned.append(path)
            interactions.extend(records)
    if not interactions:
        return None
    return build_envelope(
        probe="api_access",
        lane="APP_HTTP",
        result="PASSED",
        extracted_fact={"discovery_data": {"api_interactions": interactions[:_ACCESS_MAX_RECORDS], "files_scanned": scanned}},
        alert_rule="RemoteApiAccessObserved",
        alert_hint=f"[{hostname}] observed {len(interactions)} aggregated API access shapes",
        symptom_group="onboarding_discovery",
        namespace=hostname,
        evidence_source="DiscoveryEvidence",
        signal_type="DISCOVERY",
    )
