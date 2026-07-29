"""Form Telegram THỐNG NHẤT cho mọi lane của Omni (resource / state / app_log / siem).

Một form duy nhất, bốn lane: header badge + WHAT/WHO/WHEN/WHY/HOW-TO + Dự báo + footer Audit.
Plain-text Markdown V1. Mục tiêu: dễ đọc, dễ hiểu, dễ thao tác, dễ audit.

- Advisory card (telegram_advisory_emitter) dùng chung ``render_audit_footer`` cho footer.
- SIEM card và Contrast card render trọn vẹn qua ``render_unified_card``.

Nhãn section (tiếng Việt) là CANONICAL — ngắn gọn, mọi lane phải dùng đúng các hằng số
``LBL_*`` bên dưới (KHÔNG hard-code chuỗi nhãn ở nơi khác).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Canonical section labels — short, scannable, shared by ALL lanes (advisory + unified).
LBL_WHAT = "Sự cố"
LBL_WHERE = "Workload"
LBL_WHEN = "Thời điểm"
LBL_WHY = "Kiểm chứng"
LBL_HOWTO = "Khắc phục"
LBL_FORECAST = "Dự báo"
LBL_AUDIT = "Audit"
LBL_MEMORY = "Trí nhớ"

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
    lines: list[str] = [f"🧾 {b(LBL_AUDIT)}"]
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
# Trí nhớ — lần thứ N của cùng một pattern.
# Section THÊM MỚI, đặt ngay dưới header. Không đụng vào các marker máy
# WHAT/WHO/WHY/HOW-TO ở bất kỳ đâu (chúng bị parse ở đầu kia).
# ---------------------------------------------------------------------------

_PRIOR_VERDICT_LABEL: dict[str, str] = {
    "CORRECT": "đã xác nhận chẩn đoán ĐÚNG",
    "INCORRECT": "đã bị đánh giá SAI",
    "PARTIAL": "được đánh giá đúng nhưng thiếu",
}


def _recurrence_urgency(occurrence_no: int) -> str:
    """Lần 5 không thể trình bày cùng giọng lần 1 — nếu vẫn tái diễn thì khắc phục tạm
    đang thay chỗ cho xử lý gốc, và giọng của thẻ phải nói ra điều đó."""
    if occurrence_no >= 5:
        return "🚨 Lặp lại kéo dài — khắc phục tạm không còn tác dụng, cần xử lý nguyên nhân gốc"
    if occurrence_no >= 3:
        return "⚠️ Lặp lại nhiều lần — cần rà lại cách xử lý trước đó"
    return "🔁 Đã từng xảy ra — không phải phát hiện mới"


def render_recurrence_notice(memory: dict | None, *, markdown: bool = True) -> str:
    """Dòng trí nhớ cho advisory. Rỗng khi đây là lần đầu hoặc không có sổ ca.

    Lần ≥2 phải nói rõ "đây là lần thứ N, đã báo ngày X, ca trước xử lý tới đâu" thay vì
    trình bày lại như một phát hiện mới — điều tra lại từ đầu là vứt bỏ kinh nghiệm lần 1.
    """
    if not memory:
        return ""
    try:
        occurrence_no = int(memory.get("occurrence_no") or 0)
    except (TypeError, ValueError):
        return ""
    if occurrence_no < 2:
        return ""

    e = esc if markdown else (lambda x: str(x))
    b = (lambda s: f"*{s}*") if markdown else (lambda s: s)
    lines = [f"{b(LBL_MEMORY)} — lần thứ {occurrence_no}", f"• {e(_recurrence_urgency(occurrence_no))}"]

    opened_at = memory.get("prior_opened_at")
    if opened_at:
        lines.append(f"• Lần trước đã báo: {e(opened_at)}")

    verdict = str(memory.get("prior_diagnosis_verdict") or "").upper()
    if verdict in _PRIOR_VERDICT_LABEL:
        lines.append(f"• Ca trước: {e(_PRIOR_VERDICT_LABEL[verdict])}")
    else:
        # Không ai bấm gì ở ca trước. Nói thẳng ra, vì im lặng không phải là đồng ý và
        # cũng không phải là đã xử lý.
        lines.append("• Ca trước: CHƯA có ai phán quyết")
    if memory.get("prior_recurred"):
        lines.append("• Sự cố đã được ghi nhận tái diễn")
    prior_case = str(memory.get("prior_case_id") or "")
    if prior_case:
        # Nhãn phải khác dòng phán quyết ở trên — hai dòng cùng mở đầu "Ca trước:"
        # khiến người đọc tưởng dòng sau đính chính dòng trước.
        lines.append(f"• Mã ca trước: {short_trace(prior_case)}")
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
        parts.append(f"*{LBL_WHAT}*\n• {esc(card.what)}")
    if card.where:
        parts.append(f"*{LBL_WHERE}*\n• {esc(card.where)}")
    if card.when:
        parts.append(f"*{LBL_WHEN}*\n• {esc(card.when)}")
    if card.why:
        parts.append(f"*{LBL_WHY}*\n" + "\n".join(f"• {w}" for w in card.why))
    if card.how_to:
        parts.append(f"*{LBL_HOWTO}*\n" + "\n".join(f"• {h}" for h in card.how_to))
    if card.forecast:
        fl = [f"*{LBL_FORECAST}*"]
        for tf, sev, pred in card.forecast:
            label = (sev or "").upper()
            fl.append(f"• +{tf}: [{label}] {pred}" if pred else f"• +{tf}: [{label}]")
        parts.append("\n".join(fl))
    parts.append(render_audit_footer(card.trace_id, card.audit))
    return "\n\n".join(p for p in parts if p)
