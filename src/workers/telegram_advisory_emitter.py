"""Telegram emitter for Advisory Mode — renders AnalystAdvisory to plain-text Markdown."""

from __future__ import annotations

import json
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


_LAYER_BADGE = {
    "os_baremetal": "[L1 - OS]",
    "network":      "[L2 - Network]",
    "kubernetes":   "[L3 - K8s]",
    "prometheus":   "[L4 - Prometheus]",
}

_SEVERITY_LABEL = {
    "healthy":      "HEALTHY",
    "degraded":     "DEGRADED",
    "critical":     "CRITICAL",
    "catastrophic": "CATASTROPHIC",
}


def _render_verdict_header(advisory: AnalystAdvisory) -> str:
    lines = [
        f"*VERDICT:* {_e(advisory.verdict)}",
        f"*ROOT CAUSE:* {_e(advisory.root_cause)}",
        f"*CONFIDENCE:* {_e(advisory.confidence)}",
    ]
    if advisory.affected_workload and advisory.affected_workload != "unknown":
        lines.append(f"*WORKLOAD:* `{advisory.affected_workload}`")
    return "\n".join(lines)


def _render_verification_steps(steps: list[VerificationStep]) -> str:
    if not steps:
        return ""
    lines = ["*VERIFICATION STEPS (read-only):*"]
    for step in sorted(steps, key=lambda x: x.order):
        badge = _LAYER_BADGE.get(step.layer, f"[{step.layer}]")
        lines.append(f"\n{badge} *Step {step.order}:* {_e(step.rationale)}")
        lines.append(f"```\n{step.command}\n```")
        if step.expected_output:
            lines.append(f"Expected: `{step.expected_output[:120]}`")
    return "\n".join(lines)


def _render_remediation_steps(steps: list[ProposedRemediationStep]) -> str:
    if not steps:
        return ""
    lines = ["*PROPOSED REMEDIATION (advisory — requires human approval):*"]
    for step in sorted(steps, key=lambda x: x.order):
        approval_tag = "[APPROVAL REQUIRED]" if step.approval_required else "[OK]"
        lines.append(f"\n{approval_tag} *Step {step.order}:* {_e(step.action)}")
        if step.args:
            args_str = json.dumps(step.args, indent=2)
            lines.append(f"```\n{args_str}\n```")
        if step.preconditions:
            lines.append(f"Preconditions: {_e(', '.join(step.preconditions))}")
        if step.rollback_plan:
            lines.append(f"Rollback: `{step.rollback_plan}`")
    return "\n".join(lines)


def _render_forecast(forecast: ForecastTimeline) -> str:
    lines = [f"*IMPACT FORECAST ({_e(forecast.method)}):*"]
    if forecast.basis:
        lines.append(f"Basis: {_e(forecast.basis)}")
    if forecast.note:
        lines.append(f"Note: {_e(forecast.note)}")
    lines.append("")
    for f in sorted(forecast.forecasts, key=lambda x: int(x.timeframe[:-1])):
        label = _SEVERITY_LABEL.get(f.severity, f.severity.upper())
        lines.append(
            f"*{_e(f.timeframe)}* {label} [{_e(f.confidence)}]: {_e(f.prediction)}"
        )
    return "\n".join(lines)


def _render_escalation(reason: str) -> str:
    if not reason:
        return ""
    return f"\n*ESCALATION REQUIRED:*\n{_e(reason)}"


async def render_advisory_to_telegram(
    ctx: WorkerHandlerContext,
    advisory: AnalystAdvisory,
    chat_id: int,
) -> None:
    """Render AnalystAdvisory to Telegram as plain-text Markdown."""
    if not ctx.telegram:
        logger.warning("event=render_advisory_telegram_disabled")
        return

    parts = [
        _render_verdict_header(advisory),
        _render_verification_steps(advisory.verification_steps),
        _render_remediation_steps(advisory.proposed_remediation),
        _render_forecast(advisory.forecast),
        _render_escalation(advisory.escalation_reason),
    ]
    message = "\n\n".join([p for p in parts if p])

    if len(message) <= 4000:
        try:
            await ctx.telegram.send_message(chat_id, message, parse_mode="Markdown")
            logger.info("event=advisory_telegram_sent chat_id=%s trace=%s", chat_id, advisory.trace_id)
        except Exception as e:
            logger.error("event=advisory_telegram_send_error chat_id=%s trace=%s err=%r", chat_id, advisory.trace_id, e)
    else:
        chunks = [message[i : i + 3800] for i in range(0, len(message), 3800)]
        for idx, chunk in enumerate(chunks):
            try:
                header = f"[{idx + 1}/{len(chunks)}] " if len(chunks) > 1 else ""
                await ctx.telegram.send_message(
                    chat_id,
                    f"{header}{chunk}",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error(
                    "event=advisory_telegram_chunk_error chat_id=%s chunk=%s trace=%s err=%r",
                    chat_id, idx + 1, advisory.trace_id, e,
                )


async def render_advisory_batch_to_telegram(
    ctx: WorkerHandlerContext,
    advisories: list[AnalystAdvisory],
    chat_id: int,
    batch_summary: str = "",
) -> None:
    """Render multiple advisories as a batch summary."""
    if not advisories:
        return

    summary = batch_summary or f"{len(advisories)} incident(s) detected"
    message = f"*BATCH ALERT SUMMARY*\n{summary}\n\n---\n\n"

    for adv in advisories:
        message += (
            f"*{adv.verdict}* | {adv.root_cause[:100]}\n"
            f"  Workload: {adv.affected_workload or 'unknown'}\n"
            f"  {len(adv.verification_steps)} verification steps | "
            f"{len(adv.proposed_remediation)} actions\n\n"
        )

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
