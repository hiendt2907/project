"""Thu gọn evidence + alert context cho Analyst — không dump topology/raw JSON thừa."""

from __future__ import annotations

import json
import re
from typing import Any, cast

from pkg.rag.embed_utils import truncate_for_embedding

# Prompt injection patterns to strip from evidence fields before LLM ingestion.
# These patterns can appear in attacker-controlled SIEM events or log content.
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    # ChatML / special tokens used by LLM tokenizers
    re.compile(r"<\|im_(start|end|sep)\|>", re.IGNORECASE),
    re.compile(r"</s>|<s>|<\|endoftext\|>", re.IGNORECASE),
    # Classic prompt override phrases
    re.compile(
        r"(?i)\b(ignore|disregard|forget)\s+(the\s+)?(previous|prior|above|all)\s+"
        r"(instruction|command|directive|context|system\s+prompt)s?\b"
    ),
    re.compile(r"(?i)\bnew\s+(instruction|directive|system\s+prompt)\s*:", re.IGNORECASE),
    re.compile(r"(?i)\boverride\s+(previous|prior|all)\s+(instruction|command)s?\b"),
    # Role injection at line start (e.g. "\nsystem: do X")
    re.compile(r"(?im)^(system|assistant|user)\s*:\s*"),
]

# Collapse runs of more than 2 consecutive newlines (attacker padding / whitespace smuggling).
_RE_EXCESS_NEWLINES = re.compile(r"\n{3,}")


def sanitize_evidence_field(text: str) -> str:
    """Strip prompt injection patterns from a single evidence field value.

    Applies whitelist-pattern removal, not semantic analysis — fast and deterministic.
    Legitimate evidence (log lines, k8s events, metric values) is not affected.
    """
    if not text:
        return text
    s = text
    for pat in _INJECTION_PATTERNS:
        s = pat.sub(" ", s)
    s = _RE_EXCESS_NEWLINES.sub("\n\n", s)
    return s.strip()

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

    if p.startswith("k8s_clinical_") or p.startswith("k8s_events") or p.startswith("k8s_resource"):
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
    hint = sanitize_evidence_field(str(ev.get("alert_hint") or "").strip())
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
        ef_clean = sanitize_evidence_field(str(ef))
        lines.append(f"  metrics_or_facts: {ef_clean[:600]}")
    raw = sanitize_evidence_field(str(ev.get("raw") or "").strip())
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


_RE_HTTP_JUNK = re.compile(
    r"(?is)^HTTP/\d[\s\S]{0,800}?Bad Request|content-type:\s*[\w/-]+|^\s*400\s",
)


def _parse_extracted_fact_obj(ev: dict[str, Any]) -> dict[str, Any]:
    ef = ev.get("extracted_fact")
    if isinstance(ef, dict):
        return ef
    if isinstance(ef, str) and ef.strip().startswith("{"):
        try:
            o = json.loads(ef)
            return dict(o) if isinstance(o, dict) else {}
        except Exception:
            return {}
    return {}


def _alert_name_from_batch(batch: list[dict[str, Any]]) -> str:
    for b in batch:
        ar = str(b.get("alert_rule") or "").strip()
        if ar:
            return ar[:240]
    cq = str(batch[0].get("canonical_query_snippet") or "").strip() if batch else ""
    if cq.startswith("{"):
        try:
            o = json.loads(cq)
            labels = o.get("labels") if isinstance(o, dict) else None
            if isinstance(labels, dict):
                an = str(labels.get("alertname") or labels.get("alert_name") or "").strip()
                if an:
                    return an[:240]
        except Exception:
            pass
    return "unknown"


def _events_probe_ids() -> frozenset[str]:
    return frozenset({"k8s_clinical_pod_events", "k8s_events_probe"})


def filter_evidence_for_rag(batch: list[dict[str, Any]], *, max_tokens: int = 512) -> str:
    """
    Minimal anti-GIGO text for RAG embedding: alert name, container reasons from PodStatus,
    and critical events only — no raw HTTP/400 dumps.
    """
    if not batch:
        return "[RAG_QUERY] (empty batch)"

    alert_name = _alert_name_from_batch(batch)
    container_reason = ""
    status_ef: dict[str, Any] = {}
    for b in batch:
        if str(b.get("probe") or "") != "k8s_clinical_pod_status":
            continue
        status_ef = _parse_extracted_fact_obj(cast(dict[str, Any], b))
        phase = str(status_ef.get("phase") or "").strip()
        wr = status_ef.get("waiting_reasons")
        wr_s = ", ".join(str(x) for x in wr) if isinstance(wr, list) else ""
        sig = status_ef.get("container_signals")
        if isinstance(sig, list):
            sig_s = ", ".join(str(x) for x in sig[:12])
        else:
            sig_s = str(sig or "")[:500]
        container_reason = f"phase={phase} waiting_reasons=[{wr_s}] signals=[{sig_s}]"
        break

    ev_lines: list[str] = []
    for b in batch:
        pid = str(b.get("probe") or "")
        if pid not in _events_probe_ids():
            continue
        raw = str(b.get("raw") or "")
        raw = _RE_HTTP_JUNK.sub("", raw).strip()
        if raw:
            ev_lines.append(raw[:1200])

    critical = "\n---\n".join(ev_lines) if ev_lines else ""
    critical = _RE_HTTP_JUNK.sub("", critical).strip()

    probe_ids = sorted({str(b.get("probe") or "").strip() for b in batch if b.get("probe")})[:24]
    probes_csv = ",".join(probe_ids) if probe_ids else ""
    sg = str(batch[0].get("symptom_group") or "").strip() if batch else ""
    ly = str(batch[0].get("layer") or "").strip() if batch else ""

    parts = [
        "[RAG_QUERY]",
        f"alert_name={alert_name}",
        f"probes={probes_csv}" if probes_csv else "",
        f"symptom_group={sg}" if sg else "",
        f"layer={ly}" if ly else "",
        f"container_reason={container_reason[:1200]}",
        f"critical_events={critical[:2000]}",
    ]
    text = "\n".join(p for p in parts if p)
    return truncate_for_embedding(text, max_tokens=max_tokens)
