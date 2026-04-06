"""Truncated single-line previews for debug logs (redact secrets first)."""

from __future__ import annotations

import json
from typing import Any

from observability.normalize import redact


def log_preview(text: object | None, max_chars: int = 800) -> str:
    """One line, redacted, length-capped — safe for JSON log ``message`` fields."""
    s = redact(str(text if text is not None else ""))
    s = " ".join(s.split())
    if len(s) > max_chars:
        return s[: max_chars - 3] + "..."
    return s


def json_obj_preview(obj: Any, max_chars: int = 900) -> str:
    """``json.dumps`` + :func:`log_preview` for dict/list payloads."""
    try:
        raw = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        raw = str(obj)
    return log_preview(raw, max_chars=max_chars)


def alert_payload_summary(payload: dict[str, Any], max_chars: int = 900) -> str:
    """Compact alert fields for prober logs (not full Prometheus body)."""
    out: dict[str, Any] = {
        "source": payload.get("source"),
        "trace_id": payload.get("trace_id"),
    }
    data = payload.get("data")
    if isinstance(data, dict):
        alerts = data.get("alerts") or []
        if isinstance(alerts, list) and alerts and isinstance(alerts[0], dict):
            a0 = alerts[0]
            labels = a0.get("labels") if isinstance(a0.get("labels"), dict) else {}
            ann = a0.get("annotations") if isinstance(a0.get("annotations"), dict) else {}
            out["alertname"] = labels.get("alertname")
            out["namespace"] = labels.get("namespace")
            out["pod"] = labels.get("pod")
            out["summary"] = ann.get("summary") or ann.get("description")
    return log_preview(json.dumps(out, ensure_ascii=False), max_chars=max_chars)
