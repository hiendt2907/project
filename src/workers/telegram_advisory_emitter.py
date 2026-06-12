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
    "os_baremetal": "L1",
    "network":      "L2",
    "kubernetes":   "L3",
    "prometheus":   "L4",
}

_LAYER_NAME = {
    "os_baremetal": "OS",
    "network":      "Network",
    "kubernetes":   "K8s",
    "prometheus":   "Prometheus",
}

_PLACEHOLDER_RE = re.compile(r'<[^>]+>')

_SEVERITY_LABEL = {
    "healthy":      "HEALTHY",
    "degraded":     "DEGRADED",
    "critical":     "CRITICAL",
    "catastrophic": "CATASTROPHIC",
}

_VERDICT_EMOJI = {
    "CRITICAL":    "🚨",
    "URGENT":      "⚠️",
    "INVESTIGATE": "🔍",
    "NORMAL":      "✅",
}

_TIMEFRAME_ORDER = {"1h": 0, "3h": 1, "6h": 2, "12h": 3, "24h": 4}

_Z_SCORE_RE = re.compile(r'z[_\s]?(?:cpu|mem|score)?[=\s]?\+?([0-9]+(?:\.[0-9]+)?)', re.IGNORECASE)


def _fix_z_score_root_cause(root_cause: str) -> str:
    """Fix false 'exceeds threshold' claims when z-score is actually below 3.0."""
    m = _Z_SCORE_RE.search(root_cause)
    if not m:
        return root_cause
    z = float(m.group(1))
    if z >= 3.0:
        return root_cause
    if "exceed" not in root_cause.lower() and "above" not in root_cause.lower():
        return root_cause
    # Take first sentence only to avoid trailing fragment leakage (e.g. ". This indicates…")
    first_sentence = re.split(r'\.\s+[A-Z]', root_cause)[0]
    fixed = re.sub(
        r'(?:which\s+)?exceeds?\s+(?:the\s+)?(?:normal\s+)?threshold[^;,\n]*',
        'below 3σ — normal',
        first_sentence,
        flags=re.IGNORECASE,
    )
    fixed = re.sub(
        r'(?:which\s+)?(?:is\s+)?above\s+(?:the\s+)?(?:normal\s+)?threshold[^;,\n]*',
        'below 3σ — normal',
        fixed,
        flags=re.IGNORECASE,
    )
    return fixed


def _short_trace(trace_id: str) -> str:
    """Return last 8 chars of trace_id prefixed with #."""
    return f"#{trace_id[-8:]}" if trace_id else "#?"


_LANE_BADGE: dict[str, str] = {
    "resource": "RESOURCE",
    "state":    "STATE_FAIL",
    "app_log":  "APP_LOG",
    "siem":     "SIEM",
}


def _strip_placeholders(cmd: str) -> str:
    """Remove <placeholder> tokens and clean trailing punctuation."""
    cleaned = _PLACEHOLDER_RE.sub('', cmd).strip()
    return re.sub(r'[\-\s;,]+$', '', cleaned).strip()


def _truncate_cmd(cmd: str, max_len: int = 100) -> str:
    """Truncate at word boundary, stripping trailing punctuation."""
    if len(cmd) <= max_len:
        return cmd
    truncated = cmd[:max_len]
    last_space = truncated.rfind(' ')
    if last_space > max_len // 2:
        truncated = truncated[:last_space]
    return re.sub(r'[\-\s;,]+$', '', truncated).strip()


def _is_heuristic_fallback(forecast: ForecastTimeline) -> bool:
    """True when forecast is the boilerplate heuristic fallback with no real data."""
    if forecast.method != "heuristic":
        return False
    basis = (forecast.basis or "").lower()
    return "insufficient evidence" in basis


def _render_header(advisory: AnalystAdvisory, lane_label: str | None = None) -> str:
    emoji = _VERDICT_EMOJI.get(advisory.verdict or "", "🔔")
    badge = _LANE_BADGE.get((lane_label or "").lower().strip(), "")
    tier = getattr(advisory, "escalation_tier", "") or ""
    lane_part = f"[{badge}]" if badge else ""
    tier_part = f"[{tier}]" if tier and tier != "L2_SUGGEST" else ""
    badge_prefix = " ".join(filter(None, [lane_part, tier_part]))
    badge_prefix = f"{badge_prefix} " if badge_prefix else ""
    root_cause = _fix_z_score_root_cause(advisory.root_cause or "")
    title = root_cause[:70].rstrip()
    if len(root_cause) > 70:
        title += "..."
    return f"{emoji} *{badge_prefix}Cảnh báo {_e(advisory.verdict)}: {_e(title)}*"


def _render_what_happened(advisory: AnalystAdvisory) -> str:
    root_cause = _fix_z_score_root_cause(advisory.root_cause or "")
    confidence = advisory.confidence or "medium"
    lines = [
        "*Chuyện gì đang xảy ra?*",
        f"• {_e(root_cause)}",
    ]
    if confidence != "medium":
        lines.append(f"• Độ tin cậy: {_e(confidence)}")
    return "\n".join(lines)


def _render_who(advisory: AnalystAdvisory) -> str:
    workload = advisory.affected_workload or ""
    if not workload or workload.strip().lower() == "unknown":
        workload = "cụm (chưa xác định workload)"
    return f"*Ở đâu? (Workload)*\n• {_e(workload)}"


def _render_when(advisory: AnalystAdvisory) -> str:
    ts = advisory.timestamp
    if ts:
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S UTC") if hasattr(ts, "strftime") else str(ts)
    else:
        ts_str = "chưa rõ thời điểm phát hiện"
    return f"*Khi nào?*\n• {_e(ts_str)}"


def _render_why(steps: list[VerificationStep]) -> str:
    if not steps:
        return ""
    shown = sorted(steps, key=lambda x: x.order)[:3]
    lines = ["*Vì sao? (Bước kiểm chứng)*"]
    for step in shown:
        badge = _LAYER_BADGE.get(step.layer, step.layer.upper())
        layer_name = _LAYER_NAME.get(step.layer, step.layer)
        rationale = (step.rationale or "").strip()
        cmd_raw = (step.command or "").strip().split("\n")[0]
        cmd_clean = _strip_placeholders(cmd_raw)
        cmd_clean = _truncate_cmd(cmd_clean)
        line = f"• [{badge} — {layer_name}]"
        if cmd_clean:
            line += f" `{cmd_clean}`"
        if rationale:
            line += f"\n  → {_e(rationale)}"
        lines.append(line)
    return "\n".join(lines)


def _render_how_to_fix(steps: list[ProposedRemediationStep]) -> str:
    if not steps:
        return ""
    lines = ["*Cách khắc phục?*"]
    for step in sorted(steps, key=lambda x: x.order):
        approval = " [CẦN PHÊ DUYỆT]" if step.approval_required else ""
        lines.append(f"• Bước {step.order}:{approval} {_e(step.action)}")
        if step.rollback_plan:
            lines.append(f"  ↩ Hoàn tác: `{step.rollback_plan}`")
    return "\n".join(lines)


def _render_impact_if_not_fixed(forecast: ForecastTimeline) -> str:
    if _is_heuristic_fallback(forecast) or not forecast.forecasts:
        return "*Nếu không xử lý thì sao?*\n• Chưa đủ dữ liệu để dự báo chính xác"
    sorted_fc = sorted(forecast.forecasts, key=lambda x: _TIMEFRAME_ORDER.get(x.timeframe, 99))
    first = sorted_fc[0]
    label = _SEVERITY_LABEL.get(first.severity, first.severity.upper())
    prediction = (first.prediction or "").strip() or f"Trạng thái leo thang lên {label}"
    return f"*Nếu không xử lý thì sao?*\n• +{first.timeframe}: {_e(prediction)}"


def _render_forecast_projection(forecast: ForecastTimeline) -> str:
    if _is_heuristic_fallback(forecast) or not forecast.forecasts:
        return ""
    sorted_fc = sorted(forecast.forecasts, key=lambda x: _TIMEFRAME_ORDER.get(x.timeframe, 99))
    lines = ["*Dự báo tác động (EWMA):*"]
    shown = {fc.timeframe: fc for fc in sorted_fc}
    for tf in ("1h", "3h", "6h", "12h", "24h"):
        fc = shown.get(tf)
        if fc:
            label = _SEVERITY_LABEL.get(fc.severity, fc.severity.upper())
            pred = (fc.prediction or "")[:80].strip()
            lines.append(f"• {tf}:  {label} — {_e(pred)}" if pred else f"• {tf}:  {label}")
        else:
            lines.append(f"• {tf}:  —")
    return "\n".join(lines)


def _render_escalation(reason: str) -> str:
    if not reason:
        return ""
    return f"*CẦN LEO THANG:*\n{_e(reason)}"


# ---------------------------------------------------------------------------
# Backward-compat helpers — not used in main render path but referenced by tests
# ---------------------------------------------------------------------------

def _render_verdict_header(advisory: AnalystAdvisory, lane_label: str | None = None) -> str:
    return _render_header(advisory, lane_label=lane_label)


def _render_verification_steps(steps: list[VerificationStep]) -> str:
    if not steps:
        return ""
    shown = sorted(steps, key=lambda x: x.order)[:3]
    lines = ["*VERIFICATION STEPS (read-only):*"]
    for step in shown:
        badge = _LAYER_BADGE.get(step.layer, step.layer.upper())
        layer_name = _LAYER_NAME.get(step.layer, step.layer)
        cmd_raw = (step.command or "").strip().split("\n")[0]
        cmd_clean = _strip_placeholders(cmd_raw)
        cmd_clean = _truncate_cmd(cmd_clean)
        if not cmd_clean:
            continue
        lines.append(f"\n[{badge} — {layer_name}]\n```\n{cmd_clean}\n```")
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
    if _is_heuristic_fallback(forecast):
        return ""
    sorted_fc = sorted(forecast.forecasts, key=lambda x: _TIMEFRAME_ORDER.get(x.timeframe, 99))
    first = sorted_fc[0] if sorted_fc else None
    if not first:
        return ""
    label = _SEVERITY_LABEL.get(first.severity, first.severity.upper())
    return f"*FORECAST:* {_e(first.timeframe)} → {label} ({_e(forecast.method)})"


async def render_advisory_to_telegram(
    ctx: WorkerHandlerContext,
    advisory: AnalystAdvisory,
    chat_id: int,
    *,
    lane_label: str | None = None,
) -> None:
    """Render AnalystAdvisory to Telegram as plain-text Markdown.

    lane_label: one of 'resource', 'state', 'app_log', 'siem' — adds lane badge to header.
    """
    if not ctx.telegram:
        logger.warning("event=render_advisory_telegram_disabled")
        return

    parts = [
        _render_header(advisory, lane_label=lane_label),
        _render_what_happened(advisory),
        _render_who(advisory),
        _render_when(advisory),
        _render_why(advisory.verification_steps),
        _render_how_to_fix(advisory.proposed_remediation),
        _render_impact_if_not_fixed(advisory.forecast),
        _render_forecast_projection(advisory.forecast),
        _render_escalation(advisory.escalation_reason or ""),
    ]
    message = "\n\n".join([p for p in parts if p])
    # Footer for Loki cross-check and E2E harness (Telegram Bot API getUpdates assert).
    message = f"{message}\n\n*TRACE:* `{_short_trace(advisory.trace_id)}`"

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
