"""Up to N LLM steps to produce a single EXECUTE_MUTATE plan (tool_name + args) when RAG misses."""

from __future__ import annotations

import json
import logging
from typing import Any

from pkg.reasoning.reason_codes import (
    ERR_REA_HALLUCINATION_DETECTED,
    ERR_REA_SCHEMA_VIOLATION,
    ERR_SEM_CHANNEL_MISMATCH,
)
from workers.autonomous_execute import MUTATE_TOOL_ALLOWLIST, READONLY_TOOL_ALLOWLIST

logger = logging.getLogger(__name__)


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


def _parse_tool_json(raw: str) -> dict[str, Any] | None:
    s = (raw or "").strip()
    if not s:
        return None
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        o = json.loads(s[i : j + 1])
        if isinstance(o, dict) and "tool_name" in o:
            return o
    except Exception:
        return None
    return None


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


async def run_agentic_mutate_plan(
    ctx: Any,
    *,
    trace: str,
    sanitized_text: str,
    batch: list[dict[str, Any]],
    max_steps: int,
) -> dict[str, Any] | None:
    """Returns mutate plan {tool_name,args} from allowlist, or None."""
    ws = getattr(ctx, "settings", None)
    ollama = getattr(ctx, "ollama", None)
    if ws is None or ollama is None:
        return None
    model_candidates = _planner_model_candidates(ws)
    if not model_candidates:
        logger.warning("event=agentic_mutate_plan_no_model trace=%s", trace)
        return None
    probe_list = [str(x.get("probe")) for x in batch[:12]]
    base_user = (
        "Evidence summary (truncated):\n"
        f"{sanitized_text[:6000]}\n\n"
        f"Probes: {probe_list}\n\n"
        "Reply with exactly one JSON object, keys: tool_name (string), args (object). "
        f"Allowed tools: {', '.join(sorted(MUTATE_TOOL_ALLOWLIST))}. "
        "For k8s_rollout_restart, args must include namespace and deployment. "
        "If you cannot safely mutate, return {\"tool_name\":\"\",\"args\":{}}."
    )
    for step in range(max(1, int(max_steps))):
        messages = [
            {
                "role": "system",
                "content": "You output only valid JSON. No markdown fences. English.",
            },
            {"role": "user", "content": base_user + f"\n(step {step + 1}/{max_steps})"},
        ]
        try:
            for model in model_candidates:
                try:
                    resp = await ollama.chat(model=model, messages=messages, stream=False)
                except Exception as me:
                    logger.warning("agentic step %s model=%s: %s", step + 1, model, me)
                    continue
                msg = (resp or {}).get("message") or {}
                content = str(msg.get("content") or "")
                parsed = _parse_tool_json(content)
                reason = _reject_reason(parsed)
                if reason:
                    rejected_tool = ""
                    if isinstance(parsed, dict):
                        rejected_tool = str(parsed.get("tool_name") or "").strip()
                    logger.info(
                        "event=agentic_mutate_plan_reject trace=%s step=%s model=%s reason_code=%s tool=%s",
                        trace,
                        step + 1,
                        model,
                        reason,
                        rejected_tool or "na",
                    )
                    if reason == ERR_SEM_CHANNEL_MISMATCH and rejected_tool:
                        return {
                            "tool_name": "",
                            "args": {},
                            "reason_code": reason,
                            "suggested_tool": rejected_tool,
                        }
                    continue
                tn = str(parsed.get("tool_name") or "").strip()
                args = parsed.get("args") if isinstance(parsed.get("args"), dict) else {}
                logger.info(
                    "event=agentic_mutate_plan_ok trace=%s step=%s model=%s tool=%s",
                    trace,
                    step + 1,
                    model,
                    tn,
                )
                return {"tool_name": tn, "args": dict(args)}
        except Exception as e:
            logger.warning("agentic step %s: %s", step + 1, e)
    logger.info("event=agentic_mutate_plan_fail trace=%s", trace)
    return None
