"""Loki log probe: sustained HTTP errors in access/app logs (sigma bypass + business error evidence)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

# Nginx / common combined log: ... "GET /x HTTP/1.1" 503 ...
_RE_COMBINED_STATUS = re.compile(r'"\s*(?:[A-Z]+)\s+[^\s]+\s+HTTP/[^"]+"\s+(\d{3})\b')
# Envoy / key=value style
_RE_STATUS_KV = re.compile(r'(?:^|[\s,])(?:status|response_code)=["\']?(\d{3})\b', re.I)
# Fallback: space-separated 3-digit status (broad)
_RE_SPACE_STATUS = re.compile(r"\s(\d{3})\s")

# HTTP error classes
ErrorClass = Literal["5xx", "rate_limit", "client_abort", "auth_failure", "ok"]

_5XX = frozenset({500, 501, 502, 503, 504})
_RATE_LIMIT = frozenset({429})
_CLIENT_ABORT = frozenset({499})
_AUTH = frozenset({401, 403})


def classify_http_status(status: int) -> ErrorClass:
    """Map HTTP status code to an error class."""
    if status in _5XX:
        return "5xx"
    if status in _RATE_LIMIT:
        return "rate_limit"
    if status in _CLIENT_ABORT:
        return "client_abort"
    if status in _AUTH:
        return "auth_failure"
    return "ok"


def parse_http_status_from_access_line(line: str) -> int | None:
    """Extract HTTP status from access-style log line."""
    s = line.strip()
    if not s:
        return None
    m = _RE_COMBINED_STATUS.search(s)
    if m:
        return int(m.group(1))
    m = _RE_STATUS_KV.search(s)
    if m:
        return int(m.group(1))
    m = _RE_SPACE_STATUS.search(s)
    if m:
        return int(m.group(1))
    return None


_RE_JSON_STATUS = re.compile(r'"(?:status|status_code|http_status)"\s*:\s*(\d{3})\b', re.I)
_RE_JSON_LEVEL_ERR = re.compile(r'"level"\s*:\s*"error"', re.I)


def parse_app_json_line_5xx(line: str) -> bool:
    """True if JSON line indicates 5xx or level=error (conservative)."""
    s = line.strip()
    if not s.startswith("{"):
        return False
    try:
        j = json.loads(s)
    except Exception:
        m = _RE_JSON_STATUS.search(s)
        if m and int(m.group(1)) in _5XX:
            return True
        return bool(_RE_JSON_LEVEL_ERR.search(s))
    if not isinstance(j, dict):
        return False
    for k in ("status", "status_code", "http_status", "code"):
        v = j.get(k)
        if isinstance(v, int) and v in _5XX:
            return True
        if isinstance(v, str) and v.isdigit() and int(v) in _5XX:
            return True
    lev = str(j.get("level") or "").lower()
    if lev == "error":
        return True
    return False


def namespace_pod_from_batch(batch: list[dict[str, Any]]) -> tuple[str, str]:
    """Return (namespace, pod_name) from evidence batch."""
    ns, pod = "", ""
    for b in batch:
        ef_raw = b.get("extracted_fact")
        if isinstance(ef_raw, dict):
            ns = str(ef_raw.get("namespace") or "").strip() or ns
            pod = str(ef_raw.get("pod") or "").strip() or pod
        elif isinstance(ef_raw, str) and ef_raw.strip().startswith("{"):
            try:
                j = json.loads(ef_raw)
                if isinstance(j, dict):
                    ns = str(j.get("namespace") or "").strip() or ns
                    pod = str(j.get("pod") or "").strip() or pod
            except Exception:
                pass
        snip = str(b.get("canonical_query_snippet") or "").strip()
        if snip.startswith("{"):
            try:
                j = json.loads(snip)
                labels = j.get("labels") if isinstance(j, dict) else None
                if isinstance(labels, dict):
                    ns = str(labels.get("namespace") or "").strip() or ns
                    pod = str(labels.get("pod") or "").strip() or pod
            except Exception:
                pass
    return ns, pod


def _pod_regex_for_logql(pod: str) -> str:
    """Build LogQL regex for pod label; match pod prefix if replica hash varies.

    Note: LogQL uses RE2 syntax. Only special RE2 chars (. * + ? ^ $ { } [ ] | ( ) \\)
    need escaping — hyphens do NOT need escaping and using re.escape() would produce
    invalid RE2 patterns (e.g. \\- is invalid in RE2).
    """
    # Strip trailing hash suffix (e.g. "my-app-7d6b9-xyz" → "my-app-7d6b9")
    parts = pod.rsplit("-", 2)
    if len(parts) >= 3 and len(parts[-1]) in (4, 5) and len(parts[-2]) in (9, 10):
        base = "-".join(parts[:-2])
    elif len(parts) >= 2 and len(parts[-1]) in (4, 5):
        base = "-".join(parts[:-1])
    else:
        base = pod
    # Escape only RE2 special chars; preserve hyphens as-is
    _RE2_SPECIAL = re.compile(r"([.+?^${}()\[\]\\|*])")
    safe = _RE2_SPECIAL.sub(r"\\\1", base)
    return f"{safe}.*"


async def loki_query_range_lines(
    *,
    base_url: str,
    logql: str,
    start_sec: float,
    end_sec: float,
    limit: int = 500,
    timeout_sec: float = 20.0,
) -> tuple[list[str], str]:
    """Query Loki /loki/api/v1/query_range and return (lines, error)."""
    try:
        params = {
            "query": logql,
            "start": str(int(start_sec * 1_000_000_000)),
            "end": str(int(end_sec * 1_000_000_000)),
            "limit": str(limit),
            "direction": "backward",
        }
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            resp = await client.get(f"{base_url}/loki/api/v1/query_range", params=params)
        if resp.status_code != 200:
            return [], f"loki_http_{resp.status_code}"
        data = resp.json()
        results = data.get("data", {}).get("result", [])
        lines: list[str] = []
        for stream in results:
            for _ts, line in stream.get("values", []):
                lines.append(line)
        return lines, ""
    except Exception as exc:
        return [], str(exc)[:200]


# ---------------------------------------------------------------------------
# Access-log counting — 5xx (existing) + new business error classes
# ---------------------------------------------------------------------------

@dataclass
class AccessErrorCounts:
    """Parsed error counts from access-log lines."""
    total_parsed: int = 0
    count_5xx: int = 0
    count_rate_limit: int = 0   # 429
    count_client_abort: int = 0  # 499
    count_auth: int = 0          # 401, 403
    # Details for meta
    status_histogram: dict[int, int] = field(default_factory=dict)


def count_access_errors(lines: list[str]) -> AccessErrorCounts:
    """Parse access-log lines and count all business error classes."""
    counts = AccessErrorCounts()
    for ln in lines:
        st = parse_http_status_from_access_line(ln)
        if st is None:
            continue
        counts.total_parsed += 1
        counts.status_histogram[st] = counts.status_histogram.get(st, 0) + 1
        cls = classify_http_status(st)
        if cls == "5xx":
            counts.count_5xx += 1
        elif cls == "rate_limit":
            counts.count_rate_limit += 1
        elif cls == "client_abort":
            counts.count_client_abort += 1
        elif cls == "auth_failure":
            counts.count_auth += 1
    return counts


def _ratio_5xx_access(lines: list[str]) -> tuple[int, int]:
    """Returns (count_5xx, count_parsed_status)."""
    counts = count_access_errors(lines)
    return counts.count_5xx, counts.total_parsed


def _count_app_json_5xx(lines: list[str]) -> tuple[int, int]:
    bad = 0
    checked = 0
    for ln in lines:
        if not ln.strip().startswith("{"):
            continue
        checked += 1
        if parse_app_json_line_5xx(ln):
            bad += 1
    return bad, checked


@dataclass
class LogSurgeResult:
    ok: bool
    reason: str
    escalate_log_unavailable: bool
    meta: dict[str, Any]
    # Business error classification (new)
    dominant_error_class: ErrorClass = "ok"


async def evaluate_log_surge_sigma_bypass(
    *,
    loki_base_url: str,
    namespace: str,
    pod_name: str,
    window_sec: int,
    min_lines: int,
    min_ratio: float,
    line_limit: int,
    timeout_sec: float,
) -> LogSurgeResult:
    """
    Query Loki for recent logs; detect sustained HTTP errors across all business error classes.

    Error classes detected:
    - 5xx (500-504): server errors — sigma bypass
    - rate_limit (429): rate limiting — sigma bypass with rate_limit classification
    - client_abort (499): nginx client abort — informational, not sigma bypass
    - auth_failure (401/403): auth errors — sigma bypass with auth classification

    If Loki errors: escalate_log_unavailable True (caller escalates human).
    """
    import time

    ns = (namespace or "").strip()
    pod = (pod_name or "").strip()
    if not ns:
        return LogSurgeResult(False, "no_namespace", False, {})
    now = time.time()
    start = now - max(30, int(window_sec))

    pod_re = _pod_regex_for_logql(pod) if pod else ".*"
    logql_access = f'{{namespace="{ns}", pod_name=~"{pod_re}"}}'
    lines, err = await loki_query_range_lines(
        base_url=loki_base_url,
        logql=logql_access,
        start_sec=start,
        end_sec=now,
        limit=line_limit,
        timeout_sec=timeout_sec,
    )
    meta: dict[str, Any] = {
        "loki_query_access": logql_access,
        "window_sec": window_sec,
        "lines_fetched": len(lines),
    }
    if err:
        meta["loki_error"] = err
        return LogSurgeResult(False, "loki_unavailable", True, meta, "ok")

    counts = count_access_errors(lines)
    meta.update({
        "access_total_parsed": counts.total_parsed,
        "access_5xx": counts.count_5xx,
        "access_rate_limit_429": counts.count_rate_limit,
        "access_client_abort_499": counts.count_client_abort,
        "access_auth_failure_401_403": counts.count_auth,
        "status_histogram": counts.status_histogram,
    })

    n = counts.total_parsed
    if n >= min_lines:
        # 5xx check (existing — sigma bypass)
        if n > 0 and counts.count_5xx / n >= min_ratio:
            meta["source"] = "access"
            return LogSurgeResult(True, "access_5xx_sustained", False, meta, "5xx")

        # 429 rate-limit check (new — sigma bypass)
        if n > 0 and counts.count_rate_limit / n >= min_ratio:
            meta["source"] = "access_rate_limit"
            return LogSurgeResult(True, "access_rate_limit_sustained", False, meta, "rate_limit")

        # 401/403 auth failure check (new — sigma bypass)
        if n > 0 and counts.count_auth / n >= min_ratio:
            meta["source"] = "access_auth_failure"
            return LogSurgeResult(True, "access_auth_failure_sustained", False, meta, "auth_failure")

    # App JSON 5xx fallback
    bad_j, checked_j = _count_app_json_5xx(lines)
    meta["json_5xx_or_error"] = bad_j
    meta["json_lines_checked"] = checked_j
    if checked_j >= min_lines and bad_j / max(checked_j, 1) >= min_ratio:
        meta["source"] = "app_json"
        return LogSurgeResult(True, "app_json_5xx_sustained", False, meta, "5xx")

    # 499 client abort — informational only (do not trigger sigma bypass, but report)
    if counts.total_parsed >= min_lines and counts.count_client_abort / max(counts.total_parsed, 1) >= min_ratio:
        meta["source"] = "access_client_abort"
        meta["note"] = "499 client_abort dominates — likely upstream timeout or client disconnect; not a server error"
        return LogSurgeResult(False, "access_client_abort_informational", False, meta, "client_abort")

    return LogSurgeResult(False, "insufficient_error_evidence", False, meta, "ok")
