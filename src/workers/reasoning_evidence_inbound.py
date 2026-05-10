"""Diagnostic evidence → **read-only** LLM reasoning. No ``pkg.executor``, no tool registry, no ``handle_inbound_payload``."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import re

from pkg.reasoning.sre_output import compact_sre_diagnosis

from workers import llm_prompts_en as ope
from workers.baseline_snapshot import fetch_baseline_system_prompt
from workers.handler_context import WorkerHandlerContext
from pkg.reasoning.two_channel_sdk import parse_two_channel_sdk_only

from workers.llm_context_budget import effective_reply_max_words, truncate_to_words
from workers.infra_context import enrich_working_text_with_infra, fetch_k8s_expert_context_for_diagnostic
from workers.infra_preflight import preflight_infra_kb
from workers.log_preview import log_preview
from workers.llm_trace import log_llm_trace
from workers.metrics_exporter import inc_llm_requests
from workers.request_trace import log_end_request_ctx, log_start_request_ctx

logger = logging.getLogger(__name__)

_RE_IDENTITY_HINT = re.compile(
    r"\bnamespace[=:]\s*([\w.-]+)|\bns[=:]\s*([\w.-]+)|\bpod[=:]\s*([\w.-]+)",
    re.I,
)


def _identity_from_batch(batch: list[dict[str, Any]]) -> dict[str, str]:
    """Extract namespace/pod/deployment from batch (top-level fields or canonical_query_snippet)."""
    ns, pod, dep = "", "", ""
    for b in batch:
        ns = ns or str(b.get("namespace") or "").strip()
        pod = pod or str(b.get("pod") or b.get("pod_name") or "").strip()
        dep = dep or str(b.get("deployment") or "").strip()
        snip = str(b.get("canonical_query_snippet") or "").strip()
        if snip.startswith("{"):
            try:
                j = json.loads(snip)
                labels = j.get("labels") if isinstance(j, dict) else None
                if isinstance(labels, dict):
                    ns = ns or str(labels.get("namespace") or "").strip()
                    pod = pod or str(labels.get("pod") or labels.get("pod_name") or "").strip()
                    dep = dep or str(
                        labels.get("deployment")
                        or labels.get("deployment_name")
                        or labels.get("workload")
                        or ""
                    ).strip()
            except Exception:
                pass
    out: dict[str, str] = {}
    if ns:
        out["namespace"] = ns
    if pod:
        out["pod"] = pod
    if dep:
        out["deployment"] = dep
    return out


def _build_identity_prefix(identity: dict[str, str]) -> str:
    """Format a compact [AVAILABLE_IDENTITY] block injected before the user text."""
    if not identity:
        return ""
    lines = ["[AVAILABLE_IDENTITY]"]
    for k in ("namespace", "deployment", "pod"):
        v = identity.get(k)
        if v:
            lines.append(f"  {k}: {v}")
    lines.append("[END_AVAILABLE_IDENTITY]")
    return "\n".join(lines) + "\n\n"


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


def _llm_message_content(resp: dict[str, Any]) -> str:
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
        resp = await ctx.llm.chat(
            model=model,
            messages=[
                {"role": "system", "content": system[:16000]},
                {"role": "user", "content": working_text[:24000]},
            ],
            options=chat_opts if chat_opts else None,
        )
        raw_llm = _llm_message_content(resp)
        if not raw_llm.strip():
            out = (
                "ANALYST_EMPTY: Ollama returned no text — check model load and num_predict. "
                "Compare SDK PodMetrics vs alert; if SDK shows ~0% CPU, classify FALSE_ALARM / STALE_METRIC."
            )
        else:
            out = compact_sre_diagnosis(
                ope.truncate_plain_text_to_max_words(
                    raw_llm.strip(),
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
    "Do NOT write prose explanations — the pipeline needs executable structure.\n\n"
    "PARTIAL_IDENTITY_POLICY (read carefully):\n"
    "  If [AVAILABLE_IDENTITY] block is present with known namespace/deployment/pod, you MUST output a "
    "  read-only discovery tool scoped to that identity — NOT an empty ESCALATE action.\n"
    "  - namespace only → action.tool=\"kubectl_get_pods\" args={\"namespace\": \"<ns>\"}\n"
    "  - namespace+deployment → action.tool=\"k8s_describe_resource\" args={\"namespace\": \"<ns>\", \"name\": \"<dep>\", \"kind\": \"Deployment\"}\n"
    "  - namespace+pod → action.tool=\"k8s_describe_resource\" args={\"namespace\": \"<ns>\", \"name\": \"<pod>\", \"kind\": \"Pod\"}\n"
    "  ESCALATE is only valid when [AVAILABLE_IDENTITY] is absent or ALL identity fields are empty.\n\n"
    "Output format (strict, English):\n"
    "MACHINE_JSON: {\"verdict\":\"DIAGNOSE\"|\"ESCALATE\",\"hypothesis\":\"...\",\"action\":{\"tool\":\"\",\"args\":{}}}\n"
    "HUMAN_SUMMARY: <at most 30 words, one line — facts only, no how-to>\n\n"
    "MACHINE_JSON must be one line, max 600 characters. "
    "verdict=DIAGNOSE is INVALID unless action.tool is non-empty AND args are consistent with evidence. "
    "For credential or secret mismatch signals in the evidence, action.tool should be a real mutate/discovery "
    "from the SDK allowlist (e.g. workload describe, secret patch) with structured args — never an empty action. "
    "If unsure which tool, use verdict ESCALATE so the agentic planner runs next."
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
    log_llm_trace(
        ctx.settings,
        trace=trace,
        phase="rag_miss_sdk_only_prompt_contract",
        model=model,
        parse_ok=True,
        detail=(
            f"system_len={len(system[:16000])} user_len={len(raw_user_text[:24000])} "
            f"max_words={max_words} temperature=0.1 num_predict=512"
        ),
        raw_response=(
            "[SYSTEM_PROMPT_EXCERPT]\n"
            f"{system[:2200]}\n\n"
            "[USER_PROMPT_EXCERPT]\n"
            f"{raw_user_text[:2200]}"
        ),
    )
    resp = await ctx.llm.chat(
        model=model,
        messages=[
            {"role": "system", "content": system[:16000]},
            {"role": "user", "content": raw_user_text[:24000]},
        ],
        options={"num_predict": 512, "temperature": 0.1},
    )
    raw_llm = _llm_message_content(resp)
    log_llm_trace(
        ctx.settings,
        trace=trace,
        phase="rag_miss_sdk_only_raw",
        model=model,
        raw_response=raw_llm,
        parse_ok=bool(raw_llm.strip()),
        detail=f"user_text_len={len(raw_user_text)}",
    )
    if not raw_llm.strip():
        return {
            "human": "Empty model output.",
            "machine": {"verdict": "ESCALATE", "hypothesis": "LLM empty", "action": {}},
            "raw_llm": "",
            "display_line": "ANALYST_EMPTY: Ollama returned no text.",
        }

    parsed = parse_two_channel_sdk_only(raw_llm)
    human = truncate_to_words(parsed.get("human") or raw_llm, max_words)
    machine = parsed.get("machine")
    mach_parse_ok = isinstance(machine, dict) and bool(machine)
    log_llm_trace(
        ctx.settings,
        trace=trace,
        phase="rag_miss_sdk_only_two_channel",
        model=model,
        parse_ok=mach_parse_ok,
        detail=(
            f"has_MACHINE_JSON={'MACHINE_JSON:' in raw_llm} has_HUMAN={'HUMAN_SUMMARY:' in raw_llm} "
            f"machine_type={type(machine).__name__} "
            f"machine_json={json.dumps(machine, ensure_ascii=False)[:1800] if isinstance(machine, dict) else str(machine)}"
        ),
    )
    if not isinstance(machine, dict):
        machine = {"verdict": "ESCALATE", "hypothesis": "unparseable machine json", "action": {}}
    verdict_label = str(machine.get("verdict") or "").upper()
    action_obj = machine.get("action") if isinstance(machine.get("action"), dict) else {}
    action_tool = str((action_obj or {}).get("tool") or "").strip()
    action_args = action_obj.get("args") if isinstance(action_obj, dict) and isinstance(action_obj.get("args"), dict) else {}
    log_llm_trace(
        ctx.settings,
        trace=trace,
        phase="rag_miss_sdk_only_parse_labels",
        model=model,
        parse_ok=True,
        detail=(
            f"verdict={verdict_label or 'NA'} action_tool={action_tool or 'none'} "
            f"action_args_keys={','.join(sorted([str(k) for k in action_args.keys()])) or 'none'} "
            f"gigo_label={'diagnose_without_action' if verdict_label == 'DIAGNOSE' and not action_tool else 'none'}"
        ),
    )

    display = f"{human}\n[MACHINE]{json.dumps(machine, ensure_ascii=False)[:800]}"
    return {
        "human": human,
        "machine": machine,
        "raw_llm": raw_llm,
        "display_line": compact_sre_diagnosis(
            ope.truncate_plain_text_to_max_words(display.strip(), max_words=max_words),
            max_words=max_words,
        ),
    }
