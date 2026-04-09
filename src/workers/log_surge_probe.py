"""Loki log probe: sustained 5xx in access or app JSON logs (sigma bypass evidence)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Nginx / common combined log: ... "GET /x HTTP/1.1" 503 ...
_RE_COMBINED_STATUS = re.compile(r'"\s*(?:[A-Z]+)\s+[^\s]+\s+HTTP/[^"]+"\s+(\d{3})\b')
# Envoy / key=value style
_RE_STATUS_KV = re.compile(r'(?:^|[\s,])(?:status|response_code)=["\']?(\d{3})\b', re.I)
# Fallback: space-separated status at end-ish
_RE_SPACE_STATUS = re.compile(r"\s(500|503|504)\s")


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
        if m and int(m.group(1)) in (500, 503, 504):
            return True
        return bool(_RE_JSON_LEVEL_ERR.search(s))
    if not isinstance(j, dict):
        return False
    for k in ("status", "status_code", "http_status", "code"):
        v = j.get(k)
        if isinstance(v, int) and v in (500, 503, 504):
            return True
        if isinstance(v, str) and v.isdigit() and int(v) in (500, 503, 504):
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
    """Escape for LogQL regex; match pod prefix if replica hash varies."""
    p = (pod or "").strip()
    if not p:
        return ".*"
    if re.match(r"^[a-z0-9.-]+$", p, re.I):
        base = p.rsplit("-", 2)[0] if "-" in p else p
        return re.escape(base) + r"[\w.-]*"
    return re.escape(p)


async def loki_query_range_lines(
    *,
    base_url: str,
    logql: str,
    start_sec: float,
    end_sec: float,
    limit: int,
    timeout_sec: float,
) -> tuple[list[str], str | None]:
    """
    Returns (lines, error_message). error_message set on HTTP/parse failure.
    """
    url = f"{base_url.rstrip('/')}/loki/api/v1/query_range"
    params = {
        "query": logql,
        "limit": str(limit),
        "start": str(int(start_sec * 1e9)),
        "end": str(int(end_sec * 1e9)),
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as hc:
            r = await hc.get(url, params=params)
            r.raise_for_status()
            body = r.json()
    except Exception as e:
        return [], str(e)[:500]
    lines: list[str] = []
    for stream in (body.get("data") or {}).get("result") or []:
        for ts_line in stream.get("values") or []:
            if len(ts_line) >= 2:
                lines.append(str(ts_line[1]))
    return lines, None


def _ratio_5xx_access(lines: list[str]) -> tuple[int, int]:
    """Returns (count_5xx, count_parsed_status)."""
    bad = 0
    parsed = 0
    for ln in lines:
        st = parse_http_status_from_access_line(ln)
        if st is not None:
            parsed += 1
            if st in (500, 503, 504):
                bad += 1
    return bad, parsed


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
    Query Loki for recent logs; require sustained 500/503/504 in access or JSON app lines.
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
    # Prefer access-style: raw lines often include status triple
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
        return LogSurgeResult(False, "loki_unavailable", True, meta)

    bad_a, parsed_a = _ratio_5xx_access(lines)
    meta["access_5xx"] = bad_a
    meta["access_status_parsed"] = parsed_a
    if parsed_a >= min_lines and bad_a / max(parsed_a, 1) >= min_ratio:
        meta["source"] = "access"
        return LogSurgeResult(True, "access_5xx_sustained", False, meta)

    bad_j, checked_j = _count_app_json_5xx(lines)
    meta["json_5xx_or_error"] = bad_j
    meta["json_lines_checked"] = checked_j
    if checked_j >= min_lines and bad_j / max(checked_j, 1) >= min_ratio:
        meta["source"] = "app_json"
        return LogSurgeResult(True, "app_json_5xx_sustained", False, meta)

    return LogSurgeResult(False, "insufficient_5xx_evidence", False, meta)
