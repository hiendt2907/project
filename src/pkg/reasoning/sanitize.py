"""Thu gọn evidence + alert context cho Analyst — không dump topology/raw JSON thừa."""

from __future__ import annotations

import json
import re
from typing import Any, cast

# Probes chỉ kiểm tra hạ tầng chung; không đo CPU/memory của workload cụ thể.
_GENERIC_INFRA_PROBES = frozenset(
    {
        "redis_ping",
        "redis_stream_len_inbound",
        "kafka_alerts_topic",
    }
)

# Gồm cả dạng gộp "HighCPU", "cpu 90%", millicores…
_RE_ALERT_CPU = re.compile(
    r"(highcpu|\bcpu\b|throttl|cgroup|millicore|millicores|cores?\b|usage.*%|\d+\s*%)",
    re.IGNORECASE,
)
_RE_ALERT_MEM = re.compile(r"\b(memory|oom|rss|heap)\b", re.IGNORECASE)
_RE_ALERT_REDIS = re.compile(r"\b(redis|streams?|pel)\b", re.IGNORECASE)
_RE_ALERT_KAFKA = re.compile(r"\b(kafka|topic|consumer|lag)\b", re.IGNORECASE)


def evidence_relevance_warning(alert_hint: str, probe: str) -> str | None:
    """
    Trả về chuỗi cảnh báo nếu probe không khớp ngữ nghĩa alert (GIGO guard).
    Không có cảnh báo → None.
    """
    h = (alert_hint or "").strip()
    p = (probe or "").strip().lower()
    if not h or not p:
        return None

    if p.startswith("prom_pod_"):
        return None

    if p.startswith("k8s_clinical_"):
        return None

    if p in _GENERIC_INFRA_PROBES:
        if _RE_ALERT_CPU.search(h) or _RE_ALERT_MEM.search(h):
            return (
                f"alert suggests workload resource (cpu/mem) but probe={probe!r} "
                f"only checks generic infra — not a direct metric of the alert target"
            )
        if _RE_ALERT_REDIS.search(h) and p == "redis_ping":
            return None
        if _RE_ALERT_KAFKA.search(h) and p == "kafka_alerts_topic":
            return None
    return None


def _compact_canonical_snippet(cq: str) -> str:
    """Chỉ giữ labels chính — bỏ JSON bọc dài (kể cả chuỗi ngắn)."""
    s = cq.strip()
    if s.startswith("{"):
        try:
            o = json.loads(s)
            if isinstance(o, dict):
                labels = o.get("labels")
                if isinstance(labels, dict):
                    return json.dumps(labels, ensure_ascii=False)[:500]
        except Exception:
            pass
    if len(s) <= 240:
        return s
    return s[:400] + ("..." if len(s) > 400 else "")


def format_sanitized_analyst_user_text(ev: dict[str, Any]) -> str:
    """
    Một khối text ngắn: Alert context + status/metrics/error từ evidence — không JSON indent rác.
    """
    lines: list[str] = []
    lines.append("[ALERT_CONTEXT]")
    lines.append(f"  rule: {ev.get('alert_rule') or 'n/a'}")
    hint = str(ev.get("alert_hint") or "").strip()
    if hint:
        lines.append(f"  error_hint: {hint[:700]}")
    cq = str(ev.get("canonical_query_snippet") or "").strip()
    if cq:
        lines.append(f"  labels_or_query_hint: {_compact_canonical_snippet(cq)}")
    lines.append("[EVIDENCE]")
    es = str(ev.get("evidence_source") or "").strip()
    if es:
        lines.append(f"  source: {es} ({'real-time K8s API' if es == 'K8s_SDK' else 'historical metrics' if es == 'Prometheus' else 'other'})")
    note = str(ev.get("clinical_priority_note") or "").strip()
    if note:
        lines.append(f"  note: {note[:400]}")
    lines.append(f"  status: {ev.get('result') or 'unknown'}")
    lines.append(f"  probe: {ev.get('probe') or 'unknown'}")
    lines.append(f"  symptom_group: {ev.get('symptom_group') or ''}")
    lines.append(f"  layer: {ev.get('layer') or ''}")
    ef = ev.get("extracted_fact")
    if ef is not None and str(ef).strip():
        lines.append(f"  metrics_or_facts: {str(ef)[:600]}")
    raw = str(ev.get("raw") or "").strip()
    if raw:
        lines.append(f"  error_or_raw: {raw[:600]}")
    lines.append(f"  ts: {ev.get('ts') or ''}")
    return "\n".join(lines)


def format_batch_sanitized_analyst_user_text(ev_docs: list[dict[str, Any]]) -> str:
    """Gom nhiều evidence cùng trace — một user message cho LLM."""
    blocks: list[str] = ["[BATCH_DIAGNOSTIC_EVIDENCE — ordered probes for one trace]"]
    for i, ev in enumerate(ev_docs, 1):
        p = ev.get("probe") or "?"
        blocks.append(f"\n### Probe block {i}: {p}\n")
        blocks.append(format_sanitized_analyst_user_text(cast(dict[str, Any], ev)))
    return "\n".join(blocks)
