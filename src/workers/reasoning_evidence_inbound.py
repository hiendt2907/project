"""Diagnostic evidence → **read-only** LLM reasoning. No ``pkg.executor``, no tool registry, no ``handle_inbound_payload``."""

from __future__ import annotations

import logging
import time
from typing import Any

from workers.baseline_snapshot import fetch_baseline_system_prompt
from workers.handler_context import WorkerHandlerContext
from workers.infra_context import enrich_working_text_with_infra
from workers.infra_preflight import preflight_infra_kb
from workers.metrics_exporter import inc_llm_requests
from workers.request_trace import log_end_request, log_start_request

logger = logging.getLogger(__name__)


def _ollama_message_content(resp: dict[str, Any]) -> str:
    return str(((resp.get("message") or {}).get("content") or "")).strip()


async def reason_diagnostic_evidence_only(
    ctx: WorkerHandlerContext,
    payload: dict[str, Any],
    trace: str,
) -> str:
    """Single-shot RCA-style analysis for ``source=diagnostic_evidence`` — no mutations."""
    raw_user_text = str(payload.get("text") or "").strip()
    if not raw_user_text:
        return "Không có nội dung text."
    if not ctx.scout_ready.is_set():
        return "Em đang hoàn tất Deep Scout baseline — đại ca thử lại sau vài giây."

    t0 = time.perf_counter()
    log_start_request(
        trace,
        phase="reason_diagnostic_evidence_only",
        source="diagnostic_evidence",
        chat_id=payload.get("chat_id"),
        text_len=len(raw_user_text),
    )
    err: BaseException | None = None
    out: str | None = None
    try:
        learned = await preflight_infra_kb(ctx, raw_user_text)
        try:
            working_text = await enrich_working_text_with_infra(ctx, raw_user_text, learned=learned)
        except Exception as e:
            logger.debug("[%s] enrich skip: %s", trace, e)
            working_text = raw_user_text

        baseline = ""
        if ctx.settings.baseline_snapshot_enabled:
            baseline = await fetch_baseline_system_prompt(
                ctx.redis, ctx.settings.baseline_system_prompt_max_chars
            )
        system = (
            (baseline or "").strip()
            + "\n\n[MODE: DIAGNOSTIC_EVIDENCE — read-only analyst]\n"
            "Analyze the evidence. Do **not** propose or assume executed kubectl write, rollout, "
            "or shell mutations. Give root-cause hypotheses, verification steps, and safe human-in-the-loop next steps."
        )
        model = ctx.settings.model_reasoning_engine
        inc_llm_requests()
        resp = await ctx.ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": system[:16000]},
                {"role": "user", "content": working_text[:24000]},
            ],
            keep_alive=ctx.settings.ollama_keep_alive,
        )
        out = _ollama_message_content(resp) or "(empty model output)"
        return out
    except BaseException as e:
        err = e
        raise
    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        log_end_request(
            trace,
            phase="reason_diagnostic_evidence_only",
            status="error" if err else "ok",
            duration_ms=ms,
            out_len=len(out or ""),
            error=(f"{type(err).__name__}: {err}" if err else None),
        )
