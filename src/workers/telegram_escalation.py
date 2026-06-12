"""Single entry: human escalation via Telegram (red-button contract)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_MAX_CTX = 3500


def format_operator_triage_card(
    *,
    problem: str,
    reason: str,
    chain: list[str] | None = None,
    advise: list[str] | None = None,
) -> str:
    """Return a Problem/Reason/Chain/Advise body for Telegram escalation.

    - problem: one-line summary of WHAT is broken (alertname + resource + severity).
    - reason: WHY it broke (LLM hypothesis, or "context missing — identity incomplete").
    - chain: ordered event sequence (correlated batch, state→app_log→metrics lanes).
    - advise: concrete next actions (kubectl commands, playbook refs, oncall handoff).
    """
    lines: list[str] = []
    lines.append("Vấn đề:")
    lines.append(f"  {problem.strip() or '(không trích được mô tả vấn đề)'}")
    lines.append("")
    lines.append("Nguyên nhân:")
    lines.append(f"  {reason.strip() or '(chưa rõ nguyên nhân — cả LLM + RAG đều không đưa ra giả thuyết)'}")
    lines.append("")
    lines.append("Chuỗi sự kiện:")
    if chain:
        for i, step in enumerate(chain, start=1):
            lines.append(f"  {i}. {step}")
    else:
        lines.append("  (không có sự kiện tương quan — cảnh báo đơn lẻ)")
    lines.append("")
    lines.append("Khuyến nghị:")
    if advise:
        for s in advise:
            lines.append(f"  - {s}")
    else:
        lines.append("  - chuyển on-call — thiếu ngữ cảnh để xử lý tự động")
    return "\n".join(lines)


# Back-compat alias: older callers still import format_operator_action_card.
def format_operator_action_card(
    known_facts: dict[str, str],
    missing_facts: list[str],
    suggested_steps: list[str],
) -> str:
    problem_parts: list[str] = []
    if known_facts:
        alert = known_facts.get("alert") or "UnknownAlert"
        ns = known_facts.get("namespace") or "?"
        res = known_facts.get("deployment") or known_facts.get("pod") or "?"
        sev = known_facts.get("severity") or ""
        sev_suffix = f" [{sev}]" if sev else ""
        problem_parts.append(f"{alert} on {ns}/{res}{sev_suffix}")
    else:
        problem_parts.append("unidentified alert (no labels extracted)")
    reason_parts = [m for m in (missing_facts or []) if m.lower().startswith("llm:")]
    gaps = [m for m in (missing_facts or []) if not m.lower().startswith("llm:")]
    reason = reason_parts[0][4:].strip() if reason_parts else ""
    if not reason and gaps:
        reason = "missing context: " + ", ".join(gaps)
    return format_operator_triage_card(
        problem=" | ".join(problem_parts),
        reason=reason,
        chain=None,
        advise=suggested_steps,
    )


async def emit_telegram_escalation(
    ctx: Any,
    trace_id: str,
    context: str,
    *,
    reason: str = "ESCALATE",
) -> None:
    """
    One funnel for: max verify rounds, max mutate attempts, replan empty, ESCALATE verdict, manual escalation.
    No Redis approval workflow — notify admin chat only.
    """
    tid = str(trace_id or "").strip() or "unknown"
    ws = getattr(ctx, "settings", None)
    tg = getattr(ctx, "telegram", None)
    cid = getattr(ws, "telegram_admin_chat_id", None) if ws else None
    if tg is None or cid is None:
        logger.warning(
            "event=telegram_escalation_skip trace=%s reason=%s (no telegram or admin chat)",
            tid,
            reason,
        )
        return
    body = (context or "").strip()
    if len(body) > _MAX_CTX:
        body = body[: _MAX_CTX - 20] + "\n…(truncated)"
    msg = (
        f"[RED_ESCALATION] trace={tid}\n"
        f"reason={reason}\n"
        f"{body}"
    )
    try:
        await tg.send_message(int(cid), msg[:4000])
        logger.info("event=telegram_escalation_sent trace=%s reason=%s", tid, reason)
    except Exception as e:
        logger.warning("emit_telegram_escalation failed: %s", e)
