"""Up to N LLM steps: optional read-only ReAct, then a single EXECUTE_MUTATE plan (tool_name + args) when RAG misses."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pkg.reasoning.diagnostic_policy import DISCOVERY_TOOL_ALIASES
from pkg.reasoning.incident_matrix_profile import VALID_PROOF_LANES, pick_matrix_row_for_batch
from pkg.reasoning.reason_codes import (
    ERR_REA_HALLUCINATION_DETECTED,
    ERR_REA_SCHEMA_VIOLATION,
    ERR_SEM_CHANNEL_MISMATCH,
    PLANNER_PHASE_DONE,
)
from workers.autonomous_execute import MUTATE_TOOL_ALLOWLIST, READONLY_TOOL_ALLOWLIST

logger = logging.getLogger(__name__)

# System prompt: Observation-first + 4-stage reasoning (English, JSON-only output).
_REACT_SYSTEM = (
    "You are Omni SRE. Observation-first: do not guess causes before listing facts.\n"
    "Thought template (follow this order in every round; reflect it in your JSON choices):\n"
    "1) Start from OBSERVATION: — bullet facts grounded ONLY in the Fact Table (pods, events, errors, namespaces).\n"
    "2) Then HYPOTHESIS: — at least two plausible root causes tied to those facts.\n"
    "3) If you cannot confirm or reject a hypothesis with current facts, you MUST output JSON that runs a "
    "Discovery Tool (read-only allowlist), not a mutate tool.\n"
    "4) VERIFICATION: — use read-only tools to confirm or rule out hypotheses.\n"
    "5) FINAL_VERDICT: — emit a mutate JSON ONLY after verification supports it AND args are valid; "
    "otherwise return an empty mutate plan.\n"
    "Never emit a mutating tool as your first JSON when hard-fault evidence is present — run read-only tools first.\n"
    "Read-only tools are always executed as discovery at runtime regardless of step field.\n"
    "STRICT RULE: Your response MUST be a single valid JSON object. "
    "Do NOT include conversational filler, markdown code fences (```json), or explanations outside the JSON.\n"
    "ReAct 2.0 — include every round:\n"
    '- "phase": one of observe | verify | remediate | done\n'
    '- "analysis": short text (what you concluded from the Fact Table and prior tool output)\n'
    "When phase is done, set tool_name to empty string and explain in analysis why no further mutate is appropriate "
    "(e.g. missing ConfigMap — fix source of truth, not restart).\n"
    "You output only valid JSON. No markdown fences. English."
)


def _normalize_describe_resource_type(value: str | None) -> str | None:
    """Map LLM kind/resource_type strings to DescribeResourceArgs Literal (Pod|Deployment|Service)."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    low = s.lower()
    if low in ("pod", "pods"):
        return "Pod"
    if low in ("deployment", "deployments"):
        return "Deployment"
    if low in ("service", "services"):
        return "Service"
    if s in ("Pod", "Deployment", "Service"):
        return s
    return None


def coerce_k8s_readonly_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """
    Best-effort arg normalization before readonly tool execution (LLM often sends kind vs resource_type).
    For k8s_describe_resource, registry expects resource_type in {Pod, Deployment, Service}.
    ConfigMap is not supported by the describe tool — log and leave args unchanged.
    """
    if tool_name != "k8s_describe_resource":
        return dict(args or {})
    out = dict(args or {})
    rt_raw = out.get("resource_type")
    norm = _normalize_describe_resource_type(rt_raw) if isinstance(rt_raw, str) else None
    if norm:
        out["resource_type"] = norm
        for k in ("kind", "resource_kind", "api_kind"):
            out.pop(k, None)
        return out
    for key in ("kind", "resource_kind", "api_kind"):
        raw = out.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        stripped = raw.strip()
        cand = _normalize_describe_resource_type(stripped)
        if cand:
            out["resource_type"] = cand
            out.pop(key, None)
            for k2 in ("kind", "resource_kind", "api_kind"):
                out.pop(k2, None)
            logger.info(
                "event=k8s_args_coerced tool=k8s_describe_resource field=%s to_resource_type=%s",
                key,
                cand,
            )
            return out
        low = stripped.lower()
        if low in ("configmap", "configmaps"):
            logger.info(
                "event=k8s_args_coerce_skip tool=k8s_describe_resource reason=configmap_not_in_describe_tool kind=%s",
                stripped,
            )
    return out


def _readonly_tool_router(tool_name: str) -> bool:
    """Runtime router: if tool is read-only, it never goes to mutate/executor semantics."""
    return bool(tool_name and str(tool_name).strip() in READONLY_TOOL_ALLOWLIST)


def _planner_model_candidates(ws: Any) -> list[str]:
    vals = [
        str(getattr(ws, "diag_evidence_llm_model", "") or "").strip(),
        str(getattr(ws, "model_reasoning_engine", "") or "").strip(),
        str(getattr(ws, "model_helper", "") or "").strip(),
        str(getattr(ws, "chat_model", "") or "").strip(),
    ]
    out: list[str] = []
    for v in vals:
        if v and v not in out:
            out.append(v)
    return out


def build_fact_table_prompt(batch: list[dict[str, Any]], sanitized_text: str) -> str:
    """Structured facts JSON for the planner (reduces raw log noise)."""
    rows: list[dict[str, Any]] = []
    for b in batch[:16]:
        probe = str(b.get("probe") or "")
        ef = b.get("extracted_fact")
        if isinstance(ef, dict):
            ef_s = json.dumps(ef, ensure_ascii=False)[:4000]
        elif isinstance(ef, str):
            ef_s = ef[:4000]
        else:
            ef_s = str(ef or "")[:4000]
        rows.append(
            {
                "probe": probe[:240],
                "alert_rule": str(b.get("alert_rule") or "")[:240],
                "alert_hint": str(b.get("alert_hint") or "")[:800],
                "extracted_fact_excerpt": ef_s,
            }
        )
    blob = json.dumps({"facts": rows}, ensure_ascii=False)[:12000]
    return (
        "Fact table (JSON):\n"
        f"{blob}\n\n"
        "Sanitized narrative (truncated):\n"
        f"{sanitized_text[:4500]}\n"
    )


def _parse_agentic_json(raw: str) -> dict[str, Any] | None:
    s = (raw or "").strip()
    if not s:
        return None
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        o = json.loads(s[i : j + 1])
        return o if isinstance(o, dict) else None
    except Exception:
        return None


# Backward compat: ``autonomous_feedback_loop`` imports ``_parse_tool_json``.
_parse_tool_json = _parse_agentic_json


def _reject_reason(parsed: dict[str, Any] | None) -> str:
    if not parsed:
        return ERR_REA_SCHEMA_VIOLATION
    tn = str(parsed.get("tool_name") or "").strip()
    if not tn:
        return ERR_REA_SCHEMA_VIOLATION
    if tn in READONLY_TOOL_ALLOWLIST:
        return ERR_SEM_CHANNEL_MISMATCH
    if tn not in MUTATE_TOOL_ALLOWLIST:
        return ERR_REA_HALLUCINATION_DETECTED
    args = parsed.get("args")
    if not isinstance(args, dict):
        return ERR_REA_SCHEMA_VIOLATION
    if tn == "k8s_rollout_restart":
        ns = str(args.get("namespace") or "").strip()
        dep = str(args.get("deployment") or "").strip()
        if not ns or not dep:
            return ERR_REA_SCHEMA_VIOLATION
    return ""


def _truncate_obs(text: str, cap: int) -> str:
    s = (text or "").strip()
    return s if len(s) <= cap else s[: cap - 1] + "…"


async def _execute_readonly_tool(ctx: Any, tool_name: str, args: dict[str, Any]) -> str:
    from workers.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY.get(tool_name)
    if fn is None:
        return f"[DATA] error\n[DIAGNOSIS] unknown_readonly_tool name={tool_name!r}"
    ws = getattr(ctx, "settings", None)
    cap = int(getattr(ws, "tool_output_max_chars", 1500) or 1500) if ws is not None else 1500
    try:
        out = await fn(ctx, dict(args or {}))
        return _truncate_obs(str(out), max(400, cap))
    except Exception as e:
        logger.warning("readonly_tool_failed name=%s err=%s", tool_name, e)
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


async def infer_blind_proof_lane_hint(
    ctx: Any,
    batch: list[dict[str, Any]],
    *,
    sanitized_text: str,
    rag_match_text: str | None,
) -> str | None:
    """
    When the incident matrix does not match, optionally ask local LLM for proof_lane (resource|state|app_log).
    """
    if pick_matrix_row_for_batch(batch, rag_match_text=rag_match_text) is not None:
        return None
    ws = getattr(ctx, "settings", None)
    ollama = getattr(ctx, "ollama", None)
    if ws is None or ollama is None:
        return None
    if not bool(getattr(ws, "omni_blind_lane_llm_enabled", False)):
        return None
    model_candidates = _planner_model_candidates(ws)
    if not model_candidates:
        return None
    hint_txt = (sanitized_text or "")[:3000]
    messages = [
        {"role": "system", "content": "Reply with exactly one word: resource, state, or app_log. No punctuation."},
        {
            "role": "user",
            "content": f"Pick proof lane for diagnostic evidence (three lanes: resource, state, app_log).\n{hint_txt}",
        },
    ]
    for model in model_candidates:
        try:
            resp = await ollama.chat(model=model, messages=messages, stream=False)
            msg = (resp or {}).get("message") or {}
            raw = str(msg.get("content") or "").strip().lower()
            m = re.search(r"\b(resource|state|app_log)\b", raw)
            if m:
                w = m.group(1)
                if w in VALID_PROOF_LANES:
                    return w
        except Exception as e:
            logger.debug("blind_lane_llm model=%s err=%s", model, e)
    return None


async def run_agentic_mutate_plan(
    ctx: Any,
    *,
    trace: str,
    sanitized_text: str,
    batch: list[dict[str, Any]],
    max_steps: int,
    rag_reasoning_hints: str | None = None,
) -> dict[str, Any] | None:
    """
    Returns mutate plan {tool_name, args, discovery_steps?, reasoning_chain?, lane_hint?} or None.

    When OMNI_DIAGNOSTIC_REACT_ENABLED: interleave read-only tool JSON until a mutate JSON is returned.

    rag_reasoning_hints: optional RAG chunk text (reference only) when early SUGGEST was suppressed.
    """
    ws = getattr(ctx, "settings", None)
    ollama = getattr(ctx, "ollama", None)
    if ws is None or ollama is None:
        return None
    model_candidates = _planner_model_candidates(ws)
    if not model_candidates:
        logger.warning("event=agentic_mutate_plan_no_model trace=%s", trace)
        return None
    react_on = bool(getattr(ws, "omni_diagnostic_react_enabled", False))
    ro_max = max(0, int(getattr(ws, "omni_diagnostic_react_readonly_max", 3) or 3))
    fact_block = build_fact_table_prompt(batch, sanitized_text)
    probe_list = [str(x.get("probe")) for x in batch[:12]]
    discovery_steps: list[str] = []
    thought_process: list[str] = []
    observations: list[str] = []

    rag_block = ""
    if (rag_reasoning_hints or "").strip():
        rag_block = (
            "\nRAG reference hints (not verified — use read-only tools to confirm):\n"
            f"{str(rag_reasoning_hints).strip()[:12000]}\n\n"
        )

    base_user = (
        f"{fact_block}\n"
        f"{rag_block}"
        f"Probes: {probe_list}\n\n"
        "Stages (Observation-first): OBSERVATION (fact table) -> HYPOTHESIS (>=2) -> "
        "Verification (read-only) -> Final Verdict (mutate or abstain).\n"
        "If a hypothesis is not yet confirmable from facts, your next JSON MUST be a Discovery (read-only) tool, "
        "not a mutate.\n"
        "Reply with exactly one JSON object per round.\n"
        'If you need read-only inspection, use keys: "tool_name" (must be read-only), "args" (object), '
        '"step":"readonly". Read-only tools are always executed as discovery — even if you mistakenly set step to mutate.\n'
        'When ready to mutate after verification, use keys: "tool_name" (mutate allowlist), "args", "step":"mutate".\n'
        'If you cannot safely mutate, return {"tool_name":"","args":{},"step":"mutate"}.\n'
        'When finished with diagnosis without mutating, use phase:"done" and a clear analysis, e.g. '
        '{"phase":"done","analysis":"Root cause: missing ConfigMap X; restart would not help.","tool_name":"","args":{},"step":"mutate"}.\n'
        f"Mutate allowlist: {', '.join(sorted(MUTATE_TOOL_ALLOWLIST))}.\n"
        f"Read-only examples: {', '.join(sorted(list(READONLY_TOOL_ALLOWLIST))[:12])}...\n"
        "k8s_describe_resource args MUST use resource_type one of: Pod, Deployment, Service (exact casing); "
        "plus name and namespace. Example: "
        '{"tool_name":"k8s_describe_resource","args":{"resource_type":"Pod","name":"my-pod","namespace":"default"},'
        '"step":"readonly"}. '
        "If you use kind instead of resource_type, runtime may map kind→resource_type for those three kinds only.\n"
        "For k8s_rollout_restart, args must include namespace and deployment.\n"
    )

    total = max(1, int(max_steps))
    ro_budget = max(ro_max, 1)  # always allow at least one readonly redirect from mistaken mutate JSON
    for step in range(total):
        obs_tail = ""
        if observations:
            obs_tail = "\nPrior observations (truncated):\n" + "\n---\n".join(observations[-4:]) + "\n"
        user_content = base_user + obs_tail + f"\n(round {step + 1}/{total})\n"

        messages = (
            [
                {"role": "system", "content": _REACT_SYSTEM},
                {"role": "user", "content": user_content},
            ]
            if react_on
            else [
                {
                    "role": "system",
                    "content": _REACT_SYSTEM
                    + " When OMNI_DIAGNOSTIC_REACT_ENABLED is off, you still follow the same four stages; "
                    "output one JSON object per step.",
                },
                {"role": "user", "content": base_user + f"\n(step {step + 1}/{total})"},
            ]
        )
        try:
            parsed_any: dict[str, Any] | None = None
            for model in model_candidates:
                try:
                    resp = await ollama.chat(model=model, messages=messages, stream=False)
                except Exception as me:
                    logger.warning("agentic step %s model=%s: %s", step + 1, model, me)
                    continue
                msg = (resp or {}).get("message") or {}
                content = str(msg.get("content") or "")
                parsed_any = _parse_agentic_json(content)
                if parsed_any:
                    break
            if not parsed_any:
                continue

            step_kind = str(parsed_any.get("step") or "").strip().lower()
            tn = str(parsed_any.get("tool_name") or "").strip()
            args = parsed_any.get("args") if isinstance(parsed_any.get("args"), dict) else {}
            ph = str(parsed_any.get("phase") or "").strip().lower()
            analysis_line = str(parsed_any.get("analysis") or "").strip()
            if analysis_line:
                thought_process.append(f"[{ph or 'step'}] {analysis_line}")

            if ph == "done":
                fa = analysis_line or "Planner concluded; no automated mutate proposed."
                pl_hint_done = str(parsed_any.get("proof_lane_hint") or "").strip().lower()
                lane_done = pl_hint_done if pl_hint_done in VALID_PROOF_LANES else "unknown"
                tp_done: list[str] = list(thought_process)
                rc_done: dict[str, Any] = {
                    "verdict": "SUGGEST_FIX",
                    "lane": lane_done,
                    "thought_process": tp_done,
                }
                logger.info(
                    "event=agentic_mutate_plan_done trace=%s step=%s discovery=%s",
                    trace,
                    step + 1,
                    discovery_steps,
                )
                out_done: dict[str, Any] = {
                    "reason_code": PLANNER_PHASE_DONE,
                    "phase": "done",
                    "tool_name": "",
                    "args": {},
                    "final_analysis": fa,
                    "discovery_steps": discovery_steps,
                    "reasoning_chain": rc_done,
                }
                if pl_hint_done in VALID_PROOF_LANES:
                    out_done["lane_hint"] = pl_hint_done
                return out_done

            # Router: read-only tools always → discovery (fixes model choosing step=mutate with inspect/describe).
            if _readonly_tool_router(tn):
                if len(discovery_steps) >= ro_budget:
                    thought_process.append("readonly_max_steps_reached")
                    continue
                exec_args = coerce_k8s_readonly_args(tn, dict(args))
                obs = await _execute_readonly_tool(ctx, tn, exec_args)
                discovery_steps.append(tn)
                observations.append(f"tool={tn}\n{obs}")
                thought_process.append(
                    f"Verification (read-only {tn}): excerpt in observations; "
                    f"model_step={step_kind or 'n/a'}."
                )
                logger.info(
                    "event=readonly_discovery_redirect trace=%s step=%s tool=%s",
                    trace,
                    step + 1,
                    tn,
                )
                continue

            # mutate branch (only non-read-only tools reach here)
            parsed = parsed_any
            reason = _reject_reason(parsed)
            if reason:
                rejected_tool = str(parsed.get("tool_name") or "").strip()
                logger.info(
                    "event=agentic_mutate_plan_reject trace=%s step=%s reason_code=%s tool=%s",
                    trace,
                    step + 1,
                    reason,
                    rejected_tool or "na",
                )
                continue
            tn = str(parsed.get("tool_name") or "").strip()
            args = parsed.get("args") if isinstance(parsed.get("args"), dict) else {}
            pl_hint = str(parsed_any.get("proof_lane_hint") or "").strip().lower()
            lane_hint: str | None = None
            if pl_hint in VALID_PROOF_LANES:
                lane_hint = pl_hint
            tp: list[str] = list(thought_process) if thought_process else []
            if not tp:
                tp = [f"Planned mutate tool {tn}"] if tn else ["Empty plan"]
            tp.insert(
                0,
                "OBSERVATION: Facts from Fact Table and prior rounds; no mutate until verification when required.",
            )
            if discovery_steps:
                tp.append(f"VERIFICATION: read-only discovery tools executed: {', '.join(discovery_steps)}")
            tp.append(
                f"FINAL_VERDICT: {'EXECUTE_PLAN' if tn else 'EMPTY'} mutate_tool={tn or 'none'} "
                f"(discovery_steps={len(discovery_steps)})"
            )
            rc = {
                "verdict": "EXECUTE_PLAN" if tn else "EMPTY",
                "lane": lane_hint or "unknown",
                "thought_process": tp,
            }
            logger.info(
                "event=agentic_mutate_plan_ok trace=%s step=%s tool=%s discovery=%s",
                trace,
                step + 1,
                tn,
                discovery_steps,
            )
            out: dict[str, Any] = {
                "tool_name": tn,
                "args": dict(args),
                "discovery_steps": discovery_steps,
                "reasoning_chain": rc,
            }
            if lane_hint:
                out["lane_hint"] = lane_hint
            return out
        except Exception as e:
            logger.warning("agentic step %s: %s", step + 1, e)

    logger.info("event=agentic_mutate_plan_fail trace=%s", trace)
    return None


# Backward-compatible alias for diagnostic spec docs
def discovery_tool_registry_names_for_spec(spec_name: str) -> tuple[str, ...]:
    return DISCOVERY_TOOL_ALIASES.get(spec_name, ())
