"""Form Telegram THỐNG NHẤT cho mọi lane của Omni (resource / state / app_log / siem).

Một form duy nhất, bốn lane: header badge + WHAT/WHO/WHEN/WHY/HOW-TO + Dự báo + footer Audit.
Plain-text Markdown V1. Mục tiêu: dễ đọc, dễ hiểu, dễ thao tác, dễ audit.

- Advisory card (telegram_advisory_emitter) dùng chung ``render_audit_footer`` cho footer.
- SIEM card và Contrast card render trọn vẹn qua ``render_unified_card``.

Nhãn section (tiếng Việt) là CANONICAL — mọi lane phải khớp đúng các nhãn này:
  "Chuyện gì đang xảy ra?", "Ở đâu? (Workload)", "Khi nào?",
  "Vì sao? (Bước kiểm chứng)", "Cách khắc phục?", "Dự báo tác động".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Lane badge — single source of truth (advisory emitter re-exports for back-compat).
LANE_BADGE: dict[str, str] = {
    "resource": "RESOURCE",
    "state": "STATE_FAIL",
    "app_log": "APP_LOG",
    "siem": "SIEM",
}

VERDICT_EMOJI: dict[str, str] = {
    "CRITICAL": "🚨",
    "URGENT": "⚠️",
    "INVESTIGATE": "🔍",
    "NORMAL": "✅",
    "CONFIRMED": "🚨",
    "SUSPECTED": "🔍",
    "FALSE_ALARM": "✅",
}

_MD_ESCAPE_RE = re.compile(r"([_*`\[])")


def esc(text: object) -> str:
    """Escape Telegram Markdown V1 special chars in dynamic content."""
    return _MD_ESCAPE_RE.sub(r"\\\1", str(text).replace("\\_", "_"))


def short_trace(trace_id: str) -> str:
    """Last 8 chars of trace_id prefixed with #."""
    return f"#{trace_id[-8:]}" if trace_id else "#?"


# ---------------------------------------------------------------------------
# Audit / Decision footer — shared by ALL lanes so every card audits identically.
# ---------------------------------------------------------------------------

_MODE_LABEL: dict[str, str] = {
    "shadow": "shadow (chỉ giám sát)",
    "observe": "shadow (chỉ giám sát)",
    "minimal": "minimal (xử lý lỗi cơ bản)",
    "assist": "minimal (xử lý lỗi cơ bản)",
    "autonomous": "autonomous (tự xử lý)",
    "auto": "autonomous (tự xử lý)",
}

_DECISION_LABEL: dict[str, str] = {
    "ALLOW": "cho phép tự xử lý",
    "SUGGEST": "chỉ đề xuất (cần người duyệt)",
    "HITL": "chờ người phê duyệt (HITL)",
}


@dataclass(frozen=True)
class AuditMeta:
    """Provenance + decision + audit-chain pointer attached to every card footer."""

    mode: str | None = None  # shadow|minimal|autonomous
    decision: str | None = None  # ALLOW|SUGGEST|HITL
    origin: str | None = None  # llm|deterministic_*|recall*
    action: str | None = None  # human-readable action taken/queued
    crat_seq: int | None = None
    crat_signed: bool = False
    crat_event: str | None = None  # ADVISORY_DISPATCHED|MUTATION_ENQUEUED|...


def render_audit_footer(
    trace_id: str, audit: AuditMeta | None = None, *, markdown: bool = True
) -> str:
    """Render the canonical audit footer (always ends with TRACE for log cross-check).

    markdown=False → plain text (for callers sending with parse_mode=None, e.g. contrast digest).
    """
    e = esc if markdown else (lambda x: str(x))
    b = (lambda s: f"*{s}*") if markdown else (lambda s: s)
    code = (lambda s: f"`{s}`") if markdown else (lambda s: s)
    lines: list[str] = [f"🧾 {b('Quyết định & Audit')}"]
    if audit is not None:
        dm: list[str] = []
        if audit.mode:
            dm.append(f"Chế độ: {e(_MODE_LABEL.get(audit.mode.lower(), audit.mode))}")
        if audit.decision:
            dm.append(f"Cổng: {e(_DECISION_LABEL.get(audit.decision.upper(), audit.decision))}")
        if audit.origin:
            dm.append(f"Nguồn: {e(audit.origin)}")
        if dm:
            lines.append("• " + " · ".join(dm))
        if audit.action:
            lines.append(f"• Hành động: {e(audit.action)}")
        if audit.crat_seq is not None:
            sig = "đã ký ✓" if audit.crat_signed else "chưa ký (lab)"
            ev = f" · {e(audit.crat_event)}" if audit.crat_event else ""
            lines.append(f"• CRAT: #{audit.crat_seq} · {sig}{ev}")
    lines.append(f"{b('TRACE:')} {code(short_trace(trace_id))}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Unified card — normalized model rendered identically across lanes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnifiedCard:
    """Lane-agnostic incident card. ``why``/``how_to``/forecast preds are PRE-ESCAPED by adapters."""

    lane: str  # resource|state|app_log|siem
    verdict: str  # CONFIRMED|SUSPECTED|FALSE_ALARM|CRITICAL|...
    title: str  # one-line WHAT
    trace_id: str
    what: str = ""  # dynamic — escaped by renderer
    where: str = ""  # dynamic — escaped by renderer
    when: str = ""  # dynamic — escaped by renderer
    why: tuple[str, ...] = ()  # pre-escaped bullet lines (may contain `cmd`)
    how_to: tuple[str, ...] = ()  # pre-escaped bullet lines
    forecast: tuple[tuple[str, str, str], ...] = ()  # (timeframe, severity, prediction-pre-escaped)
    audit: AuditMeta | None = None


def _render_header(card: UnifiedCard) -> str:
    emoji = VERDICT_EMOJI.get((card.verdict or "").upper(), "🔔")
    badge = LANE_BADGE.get(card.lane.lower().strip(), card.lane.upper())
    title = (card.title or "").strip()
    if len(title) > 70:
        title = title[:70].rstrip() + "..."
    return f"{emoji} *[{badge}] Cảnh báo {esc(card.verdict)}: {esc(title)}*"


def render_unified_card(card: UnifiedCard) -> str:
    """Render a UnifiedCard to the canonical Telegram Markdown form (single form, all lanes)."""
    parts: list[str] = [_render_header(card)]
    if card.what:
        parts.append(f"*Chuyện gì đang xảy ra?*\n• {esc(card.what)}")
    if card.where:
        parts.append(f"*Ở đâu? (Workload)*\n• {esc(card.where)}")
    if card.when:
        parts.append(f"*Khi nào?*\n• {esc(card.when)}")
    if card.why:
        parts.append("*Vì sao? (Bước kiểm chứng)*\n" + "\n".join(f"• {w}" for w in card.why))
    if card.how_to:
        parts.append("*Cách khắc phục?*\n" + "\n".join(f"• {h}" for h in card.how_to))
    if card.forecast:
        fl = ["*Dự báo tác động:*"]
        for tf, sev, pred in card.forecast:
            label = (sev or "").upper()
            fl.append(f"• +{tf}: [{label}] {pred}" if pred else f"• +{tf}: [{label}]")
        parts.append("\n".join(fl))
    parts.append(render_audit_footer(card.trace_id, card.audit))
    return "\n\n".join(p for p in parts if p)
