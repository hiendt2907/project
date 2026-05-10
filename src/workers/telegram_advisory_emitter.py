"""Telegram emitter for Advisory Mode — renders AnalystAdvisory to plain-text Markdown."""

from __future__ import annotations

import asyncio
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
from workers.metrics_exporter import inc_telegram_timeout

logger = logging.getLogger(__name__)

_TELEGRAM_SEND_TIMEOUT_SEC = 10.0  # overridden by ctx.settings.telegram_send_timeout_sec


async def _tg_send(ctx: WorkerHandlerContext, chat_id: int, text: str, **kwargs: Any) -> dict:
    """Send a Telegram message with timeout guard. Raises asyncio.TimeoutError on hang."""
    timeout = float(getattr(ctx.settings, "telegram_send_timeout_sec", _TELEGRAM_SEND_TIMEOUT_SEC))
    return await asyncio.wait_for(
        ctx.telegram.send_message(chat_id, text, **kwargs),
        timeout=timeout,
    )

_MD_ESCAPE_RE = re.compile(r'([_*`\[])')


def normalize_llm_markdown_escapes(text: str) -> str:
    """Strip TeX-style ``\\_`` so Markdown does not show a stray backslash (e.g. linear\\_extrapolation)."""
    return str(text).replace("\\_", "_")


def _e(text: str) -> str:
    """Escape Telegram Markdown V1 special chars in dynamic content."""
    s = normalize_llm_markdown_escapes(str(text))
    return _MD_ESCAPE_RE.sub(r'\\\1', s)


def _evidence_shows_healthy_running_pod(evidence_text: str) -> bool:
    """True when batch text indicates Pod Running and k8s_clinical_pod_status passed (FinGuard SDK).

    Ops feedback: tune match strings here and in ``_root_cause_suggests_benign_or_below_threshold``;
    add a regression test in ``tests/test_telegram_advisory_emitter.py`` per change.
    """
    sl = evidence_text.lower()
    running = (
        "phase=running" in sl
        or '"phase": "running"' in sl
        or '"phase":"running"' in sl
        or 'phase": "running"' in evidence_text
    )
    pod_probe = "k8s_clinical_pod_status" in sl
    passed = "passed" in sl or '"result": "passed"' in sl or '"result":"passed"' in sl
    return bool(running and pod_probe and passed)


def _root_cause_suggests_benign_or_below_threshold(root_cause: str) -> bool:
    """Heuristic: benign / below-threshold wording — extend needles only with tests (ops-driven)."""
    x = root_cause.lower()
    needles = (
        "below threshold",
        "below alert threshold",
        "below the threshold",
        "healthy",
        "negligible",
        "no oom",
        "not present",
        "well below",
        "low usage",
        "low cpu",
        "low memory",
        "usage is",
        "is below",
    )
    return any(n in x for n in needles)


def copy_advisory_for_telegram_if_mismatch(
    advisory: AnalystAdvisory,
    evidence_text: str,
) -> AnalystAdvisory:
    """Operator-safe Telegram copy when the model escalates but SDK evidence shows a healthy pod.

    CRAT / ``write_audit_block`` must use the original ``advisory`` unchanged; only pass this
    return value to ``render_advisory_to_telegram``.
    """
    if advisory.verdict not in ("URGENT", "CRITICAL"):
        return advisory
    if not _evidence_shows_healthy_running_pod(evidence_text):
        return advisory
    if not _root_cause_suggests_benign_or_below_threshold(advisory.root_cause):
        return advisory

    tone_note = (
        "Telegram render: verdict and forecast toned down because live SDK shows Running/PASSED "
        "while the stated root cause describes low impact / below-threshold usage."
    )
    esc = (advisory.escalation_reason or "").strip()
    esc = f"{esc}\n({tone_note})" if esc else tone_note
    new_forecast = ForecastTimeline(
        method="heuristic",
        basis="SDK probes remained PASSED; workload phase Running.",
        forecasts=[
            ImpactForecast(
                timeframe="1h",
                severity="degraded",
                prediction=(
                    "No verified degradation path while live probes show a healthy workload; "
                    "align alert labels/PromQL with the pod and rule thresholds."
                ),
                confidence="medium",
            )
        ],
        note=(
            "Suppressed model URGENT/CRITICAL and multi-step critical/catastrophic rows for this "
            "Telegram render only (audit payload unchanged)."
        ),
    )
    logger.info(
        "event=telegram_advisory_sanitized trace_id=%s original_verdict=%s new_verdict=INVESTIGATE",
        advisory.trace_id,
        advisory.verdict,
    )
    return advisory.model_copy(
        deep=True,
        update={
            "verdict": "INVESTIGATE",
            "forecast": new_forecast,
            "escalation_reason": esc,
        },
    )


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
        lines.append(f"*WORKLOAD:* `{_e(advisory.affected_workload)}`")
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
    # Footer for Loki cross-check and E2E harness (Telegram Bot API getUpdates assert).
    message = f"{message}\n\n*TRACE:* `{_e(advisory.trace_id)}`"

    if len(message) <= 4000:
        try:
            res = await _tg_send(ctx, chat_id, message, parse_mode="Markdown")
            mid = (res.get("result") or {}).get("message_id")
            logger.info(
                "event=telegram_outbound_ok chat_id=%s message_id=%s trace=%s source=advisory_render",
                chat_id,
                mid,
                advisory.trace_id,
            )
            logger.info("event=advisory_telegram_sent chat_id=%s trace=%s", chat_id, advisory.trace_id)
        except asyncio.TimeoutError:
            inc_telegram_timeout("advisory_render")
            logger.warning(
                "event=advisory_telegram_timeout chat_id=%s trace=%s",
                chat_id, advisory.trace_id,
            )
        except Exception as e:
            logger.error("event=advisory_telegram_send_error chat_id=%s trace=%s err=%r", chat_id, advisory.trace_id, e)
    else:
        chunks = [message[i : i + 3800] for i in range(0, len(message), 3800)]
        for idx, chunk in enumerate(chunks):
            try:
                header = f"[{idx + 1}/{len(chunks)}] " if len(chunks) > 1 else ""
                res = await _tg_send(
                    ctx,
                    chat_id,
                    f"{header}{chunk}",
                    parse_mode="Markdown",
                )
                mid = (res.get("result") or {}).get("message_id")
                logger.info(
                    "event=telegram_outbound_ok chat_id=%s message_id=%s trace=%s source=advisory_chunk chunk=%s/%s",
                    chat_id,
                    mid,
                    advisory.trace_id,
                    idx + 1,
                    len(chunks),
                )
            except asyncio.TimeoutError:
                inc_telegram_timeout("advisory_chunk")
                logger.warning(
                    "event=advisory_telegram_chunk_timeout chat_id=%s chunk=%s trace=%s",
                    chat_id, idx + 1, advisory.trace_id,
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
    evidence_text: str | None = None,
) -> None:
    """Render multiple advisories as a batch summary.

    When ``evidence_text`` is set (same shape as ``sanitized_text`` in evidence_consumer), each
    advisory passed to ``render_advisory_to_telegram`` is cloned via
    ``copy_advisory_for_telegram_if_mismatch`` so Telegram stays operator-safe; CRAT must still
    use originals. When ``None``, no per-advisory sanitize is applied (callers without evidence).
    """
    if not advisories:
        return

    summary_line = batch_summary or f"{len(advisories)} incident(s) detected"
    message = f"*BATCH ALERT SUMMARY*\n{_e(summary_line)}\n\n---\n\n"

    for adv in advisories:
        rc_snip = (adv.root_cause or "")[:100]
        wl = adv.affected_workload or "unknown"
        message += (
            f"*{_e(str(adv.verdict))}* | {_e(rc_snip)}\n"
            f"  Workload: {_e(wl)}\n"
            f"  {len(adv.verification_steps)} verification steps | "
            f"{len(adv.proposed_remediation)} actions\n\n"
        )

    if len(message) > 3000:
        try:
            await _tg_send(ctx, chat_id, message, parse_mode="Markdown")
        except asyncio.TimeoutError:
            inc_telegram_timeout("batch_summary")
            logger.warning("event=advisory_batch_summary_timeout chat_id=%s", chat_id)
        except Exception as e:
            logger.error("event=advisory_batch_summary_send_error chat_id=%s err=%r", chat_id, e)

        for adv in advisories:
            tg_adv = (
                copy_advisory_for_telegram_if_mismatch(adv, evidence_text)
                if evidence_text is not None
                else adv
            )
            await render_advisory_to_telegram(ctx, tg_adv, chat_id)
    else:
        try:
            await _tg_send(ctx, chat_id, message, parse_mode="Markdown")
        except asyncio.TimeoutError:
            inc_telegram_timeout("batch_send")
            logger.warning("event=advisory_batch_send_timeout chat_id=%s", chat_id)
        except Exception as e:
            logger.error("event=advisory_batch_send_error chat_id=%s err=%r", chat_id, e)
