"""Telegram emitter for Advisory Mode — renders AnalystAdvisory to Markdown with emojis."""

from __future__ import annotations

import logging
import re
from typing import Any

from pkg.reasoning.analyst_advisory_schema import (
    AnalystAdvisory,
    ForecastTimeline,
    ImpactForecast,
    ProposedRemediationStep,
    VerificationStep,
)
from workers.handler_context import WorkerHandlerContext

logger = logging.getLogger(__name__)

_MD_ESCAPE_RE = re.compile(r'([_*`\[])')


def _e(text: str) -> str:
    """Escape Telegram Markdown V1 special chars in dynamic content."""
    return _MD_ESCAPE_RE.sub(r'\\\1', str(text))


# Emoji severity map
_VERDICT_EMOJI = {
    "NORMAL": "✅",
    "INVESTIGATE": "🔍",
    "URGENT": "⚠️",
    "CRITICAL": "🔴",
}

_SEVERITY_EMOJI = {
    "healthy": "✅",
    "degraded": "⚠️",
    "critical": "🔴",
    "catastrophic": "💥",
}

_CONFIDENCE_EMOJI = {
    "high": "🎯",
    "medium": "📊",
    "low": "❓",
}

_LAYER_BADGE = {
    "os_baremetal": "🖥️ L1",
    "network": "🌐 L2",
    "kubernetes": "☸️ L3",
    "prometheus": "📈 L4",
}


def _render_verdict_header(advisory: AnalystAdvisory) -> str:
    """Render the incident verdict + metadata."""
    emoji = _VERDICT_EMOJI.get(advisory.verdict, "❓")
    lines = [
        f"{emoji} *Verdict:* {_e(advisory.verdict)}",
        f"🔍 *Root Cause:* {_e(advisory.root_cause)}",
        f"{_CONFIDENCE_EMOJI[advisory.confidence]} *Confidence:* {_e(advisory.confidence)}",
    ]
    if advisory.affected_workload and advisory.affected_workload != "unknown":
        lines.append(f"📦 *Workload:* `{advisory.affected_workload}`")
    return "\n".join(lines)


def _render_verification_steps(steps: list[VerificationStep]) -> str:
    """Render read-only verification commands with layer badges."""
    if not steps:
        return ""
    lines = ["*🔎 Verification Steps (read-only):*"]
    for step in sorted(steps, key=lambda x: x.order):
        badge = _LAYER_BADGE.get(step.layer, "")
        lines.append(f"\n{badge} *Step {step.order}:* {_e(step.rationale)}")
        lines.append(f"```\n{step.command}\n```")
        if step.expected_output:
            lines.append(f"Expected: `{step.expected_output[:120]}`")
    return "\n".join(lines)


def _render_remediation_steps(steps: list[ProposedRemediationStep]) -> str:
    """Render proposed (advisory-only) remediation steps."""
    if not steps:
        return ""
    lines = ["*⚙️ Proposed Remediation (advisory — requires human approval):*"]
    for step in sorted(steps, key=lambda x: x.order):
        approval_icon = "🔒" if step.approval_required else "✓"
        lines.append(f"\n{approval_icon} *Step {step.order}:* {_e(step.action)}")
        if step.args:
            args_str = " ".join([f"--{k}={v}" for k, v in step.args.items()])
            lines.append(f"```\n{args_str}\n```")
        if step.preconditions:
            lines.append(f"Preconditions: {_e(', '.join(step.preconditions))}")
        if step.rollback_plan:
            lines.append(f"Rollback: `{step.rollback_plan}`")
    return "\n".join(lines)


def _render_forecast(forecast: ForecastTimeline) -> str:
    """Render time-series impact forecast."""
    lines = [f"*📈 Impact Forecast ({_e(forecast.method)}):*"]
    if forecast.basis:
        lines.append(f"Basis: {_e(forecast.basis)}")
    if forecast.note:
        lines.append(f"⚠️ Note: {_e(forecast.note)}")
    lines.append("")
    for f in sorted(forecast.forecasts, key=lambda x: int(x.timeframe[:-1])):
        emoji = _SEVERITY_EMOJI.get(f.severity, "❓")
        lines.append(
            f"{emoji} *{_e(f.timeframe)}* [{_e(f.confidence)}]: {_e(f.prediction)}"
        )
    return "\n".join(lines)


def _render_escalation(reason: str) -> str:
    """Render escalation block if present."""
    if not reason:
        return ""
    return f"\n🚨 *Escalation Required:*\n{_e(reason)}"


async def render_advisory_to_telegram(
    ctx: WorkerHandlerContext,
    advisory: AnalystAdvisory,
    chat_id: int,
) -> None:
    """
    Render AnalystAdvisory to Telegram as structured Markdown.
    Sends one or more messages depending on length.
    """
    if not ctx.telegram:
        logger.warning("event=render_advisory_telegram_disabled")
        return

    # Build Markdown message
    parts = [
        _render_verdict_header(advisory),
        _render_verification_steps(advisory.verification_steps),
        _render_remediation_steps(advisory.proposed_remediation),
        _render_forecast(advisory.forecast),
        _render_escalation(advisory.escalation_reason),
    ]
    message = "\n\n".join([p for p in parts if p])

    # Split if too long for a single Telegram message (4096 char limit)
    if len(message) <= 4000:
        try:
            await ctx.telegram.send_message(chat_id, message, parse_mode="Markdown")
            logger.info("event=advisory_telegram_sent chat_id=%s trace=%s", chat_id, advisory.trace_id)
        except Exception as e:
            logger.error("event=advisory_telegram_send_error chat_id=%s trace=%s err=%r", chat_id, advisory.trace_id, e)
    else:
        # Split into chunks
        chunks = [message[i : i + 3800] for i in range(0, len(message), 3800)]
        for idx, chunk in enumerate(chunks):
            try:
                header = (
                    f"[{idx + 1}/{len(chunks)}] "
                    if len(chunks) > 1
                    else ""
                )
                await ctx.telegram.send_message(
                    chat_id,
                    f"{header}{chunk}",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error(
                    "event=advisory_telegram_chunk_error chat_id=%s chunk=%s trace=%s err=%r",
                    chat_id,
                    idx + 1,
                    advisory.trace_id,
                    e,
                )


async def render_advisory_batch_to_telegram(
    ctx: WorkerHandlerContext,
    advisories: list[AnalystAdvisory],
    chat_id: int,
    batch_summary: str = "",
) -> None:
    """
    Render multiple advisories as a batch (e.g., 3 incidents, 1 alert escalation).
    """
    if not advisories:
        return

    # Summary line
    summary = batch_summary or f"{len(advisories)} incident(s) detected"
    message = f"📊 *Batch Alert Summary*\n{summary}\n\n---\n\n"

    # Render each advisory as a compact block
    for adv in advisories:
        emoji = _VERDICT_EMOJI.get(adv.verdict, "❓")
        message += (
            f"{emoji} *{adv.verdict}* | {adv.root_cause[:100]}\n"
            f"  Workload: {adv.affected_workload or 'unknown'}\n"
            f"  {len(adv.verification_steps)} verification steps | "
            f"{len(adv.proposed_remediation)} actions\n\n"
        )

    # If total length is large, send summary first, then individual advisories
    if len(message) > 3000:
        try:
            await ctx.telegram.send_message(chat_id, message, parse_mode="Markdown")
        except Exception as e:
            logger.error("event=advisory_batch_summary_send_error chat_id=%s err=%r", chat_id, e)

        for adv in advisories:
            await render_advisory_to_telegram(ctx, adv, chat_id)
    else:
        try:
            await ctx.telegram.send_message(chat_id, message, parse_mode="Markdown")
        except Exception as e:
            logger.error("event=advisory_batch_send_error chat_id=%s err=%r", chat_id, e)
