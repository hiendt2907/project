"""Telegram emitter for RemoteAgent multi-turn diagnosis sessions.

INVARIANT INV_TELEGRAM_FULL: All 5 sections MUST be present before send:
  1. VẤN ĐỀ (root cause)
  2. ẢNH HƯỞNG (affected components + impact)
  3. ĐÃ CHẨN ĐOÁN (commands run per turn, with outputs)
  4. CẦN LÀM (remediation steps — advisory only)
  5. TRACE + agent info

Never emits when INV_DIAG_STORED has not been satisfied (session must be in Redis).

RENDER MODE: Telegram parse_mode="HTML". HTML mode only requires escaping the
three reserved chars `<`, `>`, `&`; <code> spans are verbatim once those three
are escaped. This eliminates the entire Markdown V1 entity-balancing failure
class (e.g. underscores inside `code` spans desyncing the parser → 400
"Can't find end of the entity"). Do NOT switch back to Markdown.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from workers.handler_context import WorkerHandlerContext
from workers.metrics_exporter import inc_telegram_timeout

logger = logging.getLogger(__name__)

_SEND_TIMEOUT_S = 10.0


def _e(text: Any) -> str:
    """Escape the 3 HTML-reserved chars for Telegram HTML parse_mode.

    Order matters: ampersand first so we don't double-escape the entities
    produced for < and >.
    """
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _short_trace(trace_id: str) -> str:
    return f"#{trace_id[-8:]}" if trace_id else "#?"


def _truncate(text: str, n: int = 200) -> str:
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0] + "…"


def _render_section1_problem(final: dict[str, Any]) -> str:
    root_cause = final.get("root_cause") or "Unknown root cause"
    confidence_pct = int(float(final.get("confidence", 0)) * 100)
    return (
        f"📍 <b>VẤN ĐỀ</b>\n"
        f"{_e(_truncate(root_cause, 300))}\n"
        f"Confidence: {confidence_pct}%"
    )


def _render_section2_impact(final: dict[str, Any]) -> str:
    affected = final.get("affected_components", [])
    impact = final.get("impact_summary", "")
    lines = ["🎯 <b>ẢNH HƯỞNG</b>"]
    if affected:
        for comp in affected[:5]:
            lines.append(f"• {_e(str(comp))}")
    else:
        lines.append("• Chưa xác định được component bị ảnh hưởng")
    if impact:
        lines.append(f"\n<i>{_e(_truncate(impact, 200))}</i>")
    return "\n".join(lines)


def _render_section3_diagnosed(turns: list[dict[str, Any]]) -> str:
    total = len(turns)
    lines = [f"🔍 <b>ĐÃ CHẨN ĐOÁN ({total} turns)</b>"]
    for turn in turns:
        n = turn.get("turn", "?")
        hypothesis = _truncate(turn.get("hypothesis", ""), 100)
        lines.append(f"\n<b>Turn {n}:</b> {_e(hypothesis)}")
        for result in turn.get("command_results", []):
            # Always show the EXACT command that ran so the operator can see
            # "what Omni did" — purpose alone (free text) is not auditable.
            command_str = (result.get("command_str") or "").strip()
            purpose = result.get("purpose", "")
            purpose_str = f" <i>— {_e(_truncate(purpose, 80))}</i>" if purpose else ""
            if command_str:
                lines.append(f"  $ <code>{_e(command_str[:120])}</code>{purpose_str}")
            elif purpose:
                # Fallback for legacy sessions stored before command_str existed.
                lines.append(f"  ▸{purpose_str}")

            if result.get("blocked"):
                lines.append(f"  ⛔ <i>blocked: {_e(result.get('block_reason', 'blocked')[:60])}</i>")
                continue
            if result.get("status") == "timeout":
                # Tool never executed — surface it instead of a misleading rc=1.
                lines.append("  ⏳ <i>agent offline — lệnh chưa chạy</i>")
                continue

            rc = result.get("rc", 0)
            stdout = result.get("stdout", "").strip()
            stderr = (result.get("stderr", "") or "").strip()[:200]
            if rc == 0 and stdout:
                # Show the first 3 non-empty lines — enough to read top offenders
                # from a size-sorted (ls -lS / du) listing.
                preview = "\n".join(
                    ln for ln in stdout.splitlines() if ln.strip()
                )[:280]
                lines.append(f"  ✅\n  <code>{_e(preview)}</code>")
            elif rc == 0:
                lines.append("  ✅ <i>(không có output)</i>")
            else:
                detail = f"\n  <code>{_e(stderr)}</code>" if stderr else ""
                lines.append(f"  ❌ rc={rc}{detail}")
    return "\n".join(lines)


def _render_section4_remediation(final: dict[str, Any]) -> str:
    steps = final.get("remediation_steps", [])
    lines = ["🛠️ <b>CẦN LÀM (thực hiện thủ công, Omni không tự thực thi)</b>"]
    if not steps:
        lines.append("• Không có bước remediation cụ thể — cần điều tra thêm")
    else:
        for i, step in enumerate(steps[:8], 1):
            if isinstance(step, dict):
                action = step.get("action") or step.get("command") or str(step)
            else:
                action = str(step)
            lines.append(f"{i}. {_e(_truncate(action, 200))}")
    lines.append("\n⚠️ <i>Mọi thay đổi cần approval trước khi thực thi</i>")
    return "\n".join(lines)


def _render_section5_footer(session: dict[str, Any]) -> str:
    trace_id = session.get("trace_id", "")
    agent_id = session.get("agent_id", "")
    probe = session.get("probe", "")
    lane = session.get("lane", "")
    # <code> content is verbatim in HTML mode — underscores inside disk_usage /
    # SYS_HARD_FAIL no longer desync the parser as they did in Markdown V1.
    return (
        f"<b>TRACE:</b> <code>{_e(_short_trace(trace_id))}</code> | "
        f"agent=<code>{_e(agent_id[:32])}</code> | "
        f"probe=<code>{_e(probe)}</code> | lane=<code>{_e(lane)}</code>"
    )


def render_diagnosis_session(session: dict[str, Any]) -> str:
    """Render a complete DiagnosisSession to Telegram HTML.

    INVARIANT INV_TELEGRAM_FULL: All 5 sections present.
    """
    agent_id = session.get("agent_id", "unknown")
    alert_hint = _truncate(session.get("alert_hint", ""), 120)
    final = session.get("final", {})
    turns = session.get("turns", [])
    confidence_pct = int(float(final.get("confidence", 0)) * 100)

    # Determine severity badge
    if confidence_pct >= 80:
        badge = "🔴"
    elif confidence_pct >= 50:
        badge = "🟡"
    else:
        badge = "🔵"

    degraded_line = (
        "\n⚠️ <i>DEGRADED: agent offline — chẩn đoán chỉ dựa trên facts đã thu thập</i>"
        if session.get("degraded")
        else ""
    )
    header = (
        f"{badge} <b>[REMOTE DIAG] {_e(agent_id[:40])}</b>\n"
        f"<i>{_e(alert_hint)}</i>"
        f"{degraded_line}"
    )

    sections = [
        header,
        _render_section1_problem(final),
        _render_section2_impact(final),
        _render_section3_diagnosed(turns),
        _render_section4_remediation(final),
        _render_section5_footer(session),
    ]

    return "\n\n".join(s for s in sections if s)


async def emit_diagnosis_to_telegram(
    ctx: WorkerHandlerContext,
    session: dict[str, Any],
    chat_id: int,
) -> None:
    """Send diagnosis session to Telegram. Enforces 5-section completeness."""
    if not ctx.telegram:
        logger.warning("[diag-emit] telegram disabled — skipping")
        return

    trace_id = session.get("trace_id", "?")
    message = render_diagnosis_session(session)

    timeout = float(getattr(ctx.settings, "telegram_send_timeout_sec", _SEND_TIMEOUT_S))

    async def _send(text: str) -> None:
        await asyncio.wait_for(
            ctx.telegram.send_message(chat_id, text, parse_mode="HTML"),
            timeout=timeout,
        )

    # Chunk on a character budget that stays within Telegram's 4096-byte limit
    # even after multibyte (emoji/Vietnamese) expansion. Split on newline
    # boundaries so we never bisect an HTML tag mid-entity.
    chunks = _chunk_on_newlines(message, 3500)
    for idx, chunk in enumerate(chunks):
        prefix = f"[{idx + 1}/{len(chunks)}] " if len(chunks) > 1 else ""
        try:
            await _send(f"{prefix}{chunk}")
            logger.info(
                "[diag-emit] sent trace=%s chunk=%d/%d chat=%s",
                trace_id, idx + 1, len(chunks), chat_id,
            )
        except asyncio.TimeoutError:
            inc_telegram_timeout("diagnosis_emit")
            logger.warning(
                "[diag-emit] timeout trace=%s chunk=%d chat=%s", trace_id, idx + 1, chat_id
            )
        except Exception as exc:
            logger.error(
                "[diag-emit] send_error trace=%s chunk=%d chat=%s err=%r",
                trace_id, idx + 1, chat_id, exc,
            )


def _chunk_on_newlines(text: str, budget: int) -> list[str]:
    """Split text into <=budget-char chunks on newline boundaries.

    Avoids bisecting an HTML entity/tag, which would itself trigger a 400.
    """
    if len(text) <= budget:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > budget and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
