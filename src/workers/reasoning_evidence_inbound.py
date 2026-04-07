"""Diagnostic evidence → **read-only** LLM reasoning. No ``pkg.executor``, no tool registry, no ``handle_inbound_payload``."""

from __future__ import annotations

import logging
import time
from typing import Any

from pkg.reasoning.sre_output import compact_sre_diagnosis

from workers import ollama_prompts_en as ope
from workers.baseline_snapshot import fetch_baseline_system_prompt
from workers.handler_context import WorkerHandlerContext
from pkg.reasoning.two_channel_sdk import parse_two_channel_sdk_only

from workers.llm_context_budget import effective_reply_max_words, truncate_to_words
from workers.infra_context import enrich_working_text_with_infra, fetch_k8s_expert_context_for_diagnostic
from workers.infra_preflight import preflight_infra_kb
from workers.log_preview import log_preview
from workers.metrics_exporter import inc_llm_requests
from workers.request_trace import log_end_request_ctx, log_start_request_ctx

logger = logging.getLogger(__name__)


def _preflight_hints_from_payload(payload: dict[str, Any]) -> dict[str, str] | None:
    """Namespace/pod từ payload (alert labels / gateway) — không Redis."""
    h: dict[str, str] = {}
    ns = str(payload.get("namespace") or "").strip()
    if ns:
        h["namespace"] = ns
    pod = str(payload.get("pod_name") or payload.get("pod") or "").strip()
    if pod:
        h["pod_name"] = pod
    svc = str(payload.get("service_name") or "").strip()
    if svc:
        h["service_name"] = svc
    return h if h else None


_DIAG_NUM_PREDICT_SANITIZED = 150

# SDK block trong batch là sự thật trước Prometheus; không im lặng khi mâu thuẫn.
def _diag_system_suffix(max_words: int) -> str:
    mw = max(1, int(max_words))
    return (
        "Kubernetes SDK probe blocks above are the authoritative state machine; Prometheus lines are secondary. "
        "If probes do not match the alert semantics, reply exactly: INVALID_EVIDENCE: <reason>. "
        "If SDK shows ~0% CPU while the alert claims high CPU, reply exactly: "
        "FALSE_ALARM: <one line> STALE_METRIC: <one line>. "
        f"Otherwise at most four short lines. Total reply at most {mw} words. No filler."
    )


def _ollama_message_content(resp: dict[str, Any]) -> str:
    return str(((resp.get("message") or {}).get("content") or "")).strip()


async def reason_diagnostic_evidence_only(
    ctx: WorkerHandlerContext,
    payload: dict[str, Any],
    trace: str,
) -> str:
    """Single-shot RCA-style analysis for ``source=diagnostic_evidence`` — no mutations."""
    _ = trace  # giữ chữ ký API; trace thực tế = ContextVar (đã push ở evidence_consumer).
    raw_user_text = str(payload.get("text") or "").strip()
    if not raw_user_text:
        return "No text content."
    if not ctx.scout_ready.is_set():
        return "Deep Scout baseline not ready; retry shortly."

    t0 = time.perf_counter()
    log_start_request_ctx(
        phase="reason_diagnostic_evidence_only",
        source="diagnostic_evidence",
        chat_id=payload.get("chat_id"),
        text_len=len(raw_user_text),
        in_preview=log_preview(raw_user_text, max_chars=1200),
        batched=bool(payload.get("batched_probes")),
    )
    err: BaseException | None = None
    out: str | None = None
    try:
        sanitized = bool(payload.get("diagnostic_evidence_sanitized"))
        gate_done = bool(payload.get("rag_gate_evaluated"))
        kb_expert = ""
        if (
            sanitized
            and bool(getattr(ctx.settings, "diag_k8s_expert_rag_enabled", True))
            and not gate_done
        ):
            kb_expert = await fetch_k8s_expert_context_for_diagnostic(ctx, raw_user_text)

        if sanitized:
            if kb_expert:
                working_text = f"{kb_expert}\n\n[DIAGNOSTIC_EVIDENCE]\n{raw_user_text}"
            else:
                working_text = raw_user_text
            baseline = ""
        else:
            learned = await preflight_infra_kb(
                ctx, raw_user_text, hints=_preflight_hints_from_payload(payload)
            )
            try:
                working_text = await enrich_working_text_with_infra(ctx, raw_user_text, learned=learned)
            except Exception:
                working_text = raw_user_text

            baseline = ""
            if ctx.settings.baseline_snapshot_enabled:
                baseline = await fetch_baseline_system_prompt(
                    ctx.redis, ctx.settings.baseline_system_prompt_max_chars
                )
        max_words = effective_reply_max_words(ctx.settings)
        if sanitized:
            kb_note = ""
            if kb_expert:
                kb_note = (
                    "\n\n[CONTEXT_POLICY] Excerpts in [CONTEXT: k8s_expert] are official Kubernetes documentation "
                    "for orientation only. SDK / probe blocks in [DIAGNOSTIC_EVIDENCE] are authoritative for cluster state."
                )
            system = (
                (baseline or "").strip()
                + "\n\n[OUTPUT_LANGUAGE] English only. No Vietnamese.\n\n[DIAGNOSTIC_ANALYST]\n"
                + _diag_system_suffix(max_words)
                + kb_note
            )
            num_predict = _DIAG_NUM_PREDICT_SANITIZED
        else:
            system = (
                (baseline or "").strip()
                + "\n\n[OUTPUT_LANGUAGE] English only. No Vietnamese.\n\n"
                "[MODE: DIAGNOSTIC_EVIDENCE — read-only analyst]\n"
                "Analyze the evidence. Do **not** propose or assume executed kubectl write, rollout, "
                "or shell mutations. Give root-cause hypotheses, verification steps, and safe human-in-the-loop next steps. "
                f"Reply at most {max_words} words total. No filler."
            )
            num_predict = None
        model = ctx.settings.model_reasoning_engine
        dm = getattr(ctx.settings, "diag_evidence_llm_model", None) or ""
        if isinstance(dm, str) and dm.strip():
            model = dm.strip()
        inc_llm_requests()
        chat_opts: dict[str, Any] = {"num_predict": num_predict} if num_predict else {}
        resp = await ctx.ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": system[:16000]},
                {"role": "user", "content": working_text[:24000]},
            ],
            keep_alive=ctx.settings.ollama_keep_alive,
            options=chat_opts if chat_opts else None,
        )
        raw_ollama = _ollama_message_content(resp)
        if not raw_ollama.strip():
            out = (
                "ANALYST_EMPTY: Ollama returned no text — check model load and num_predict. "
                "Compare SDK PodMetrics vs alert; if SDK shows ~0% CPU, classify FALSE_ALARM / STALE_METRIC."
            )
        else:
            out = compact_sre_diagnosis(
                ope.truncate_plain_text_to_max_words(
                    raw_ollama.strip(),
                    max_words=max_words,
                ),
                max_words=max_words,
            )
        return out
    except BaseException as e:
        err = e
        raise
    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        end_out = out if not err else ""
        log_end_request_ctx(
            phase="reason_diagnostic_evidence_only",
            status="error" if err else "ok",
            duration_ms=ms,
            out_len=len(out or ""),
            out_preview=log_preview(end_out if not err else f"{type(err).__name__}: {err}", max_chars=1600),
            error=(f"{type(err).__name__}: {err}" if err else None),
        )


_ZERO_KNOWLEDGE_SYSTEM = (
    "[MODE: ZERO_KNOWLEDGE — RAG miss]\n"
    "You have NO retrieval-augmented knowledge for this turn. "
    "Infer ONLY from the diagnostic evidence below (Kubernetes SDK probes, structured facts in the block).\n"
    "Do NOT invent fixes from general documentation or memory. "
    "If you cannot ground a conclusion in the evidence, set verdict ESCALATE in MACHINE_JSON.\n\n"
    "Output format (strict, English):\n"
    "MACHINE_JSON: {\"verdict\":\"DIAGNOSE\"|\"ESCALATE\",\"hypothesis\":\"...\",\"action\":{\"tool\":\"\",\"args\":{}}}\n"
    "HUMAN_SUMMARY: <at most 30 words, one line>\n\n"
    "MACHINE_JSON must be one line, max 600 characters. action.tool must be empty unless you have "
    "namespace+deployment from evidence labels for an allowlisted rollout (otherwise ESCALATE)."
)


async def reason_diagnostic_rag_miss_sdk_only(
    ctx: WorkerHandlerContext,
    payload: dict[str, Any],
    trace: str,
) -> dict[str, Any]:
    """
    RAG miss + Truth Law: LLM allowed, SDK-only reasoning, two-channel output.
    Returns: human_summary, machine dict|None, raw_llm, display_line (for legacy return).
    """
    _ = trace
    raw_user_text = str(payload.get("text") or "").strip()
    if not raw_user_text:
        return {
            "human": "No evidence.",
            "machine": {"verdict": "ESCALATE", "hypothesis": "empty input", "action": {}},
            "raw_llm": "",
            "display_line": "No text content.",
        }
    if not ctx.scout_ready.is_set():
        return {
            "human": "Baseline not ready.",
            "machine": {"verdict": "ESCALATE", "hypothesis": "scout not ready", "action": {}},
            "raw_llm": "",
            "display_line": "Deep Scout baseline not ready; retry shortly.",
        }

    max_words = effective_reply_max_words(ctx.settings)
    system = (
        "[OUTPUT_LANGUAGE] English only. No Vietnamese.\n\n"
        + _ZERO_KNOWLEDGE_SYSTEM
        + f"\n\nHUMAN_SUMMARY must be at most {max_words} words."
    )
    model = ctx.settings.model_reasoning_engine
    dm = getattr(ctx.settings, "diag_evidence_llm_model", None) or ""
    if isinstance(dm, str) and dm.strip():
        model = dm.strip()
    inc_llm_requests()
    resp = await ctx.ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": system[:16000]},
            {"role": "user", "content": raw_user_text[:24000]},
        ],
        keep_alive=ctx.settings.ollama_keep_alive,
        options={"num_predict": 512, "temperature": 0.1},
    )
    raw_ollama = _ollama_message_content(resp)
    if not raw_ollama.strip():
        return {
            "human": "Empty model output.",
            "machine": {"verdict": "ESCALATE", "hypothesis": "LLM empty", "action": {}},
            "raw_llm": "",
            "display_line": "ANALYST_EMPTY: Ollama returned no text.",
        }

    parsed = parse_two_channel_sdk_only(raw_ollama)
    human = truncate_to_words(parsed.get("human") or raw_ollama, max_words)
    machine = parsed.get("machine")
    if not isinstance(machine, dict):
        machine = {"verdict": "ESCALATE", "hypothesis": "unparseable machine json", "action": {}}

    display = f"{human}\n[MACHINE]{json.dumps(machine, ensure_ascii=False)[:800]}"
    return {
        "human": human,
        "machine": machine,
        "raw_llm": raw_ollama,
        "display_line": compact_sre_diagnosis(
            ope.truncate_plain_text_to_max_words(display.strip(), max_words=max_words),
            max_words=max_words,
        ),
    }
