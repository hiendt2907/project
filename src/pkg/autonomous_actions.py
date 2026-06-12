"""Kafka contracts for remediation suggestions, Shadow OS runbooks, and action feedback."""

from __future__ import annotations

import uuid
from typing import Any

ACTION_SUGGEST_REMEDIATION = "SUGGEST_REMEDIATION"
ACTION_SUGGEST_OS_RUNBOOK = "SUGGEST_OS_RUNBOOK"
ACTION_EXECUTE_MUTATE = "EXECUTE_MUTATE"


def build_execute_mutate_body(
    trace_id: str,
    *,
    tool_name: str,
    args: dict[str, Any],
    attempt_count: int,
    correlation_id: str | None = None,
    reasoning_chain: dict[str, Any] | None = None,
    planner_origin: str | None = None,
) -> dict[str, Any]:
    """Inner Kafka JSON for omni-actions — executor runs mutate after Pre-apply when allowed."""
    cid = (correlation_id or "").strip() or str(uuid.uuid4())
    data: dict[str, Any] = {
        "tool_name": str(tool_name).strip()[:256],
        "args": dict(args) if isinstance(args, dict) else {},
        "attempt_count": max(1, int(attempt_count)),
        "correlation_id": cid,
    }
    if reasoning_chain is not None and isinstance(reasoning_chain, dict) and reasoning_chain:
        data["reasoning_chain"] = reasoning_chain
    # Reasoning-source provenance for the executor's autonomy-tier gate (minimal mode
    # only auto-runs trusted RAG/deterministic origins). Default "llm" at the executor.
    if planner_origin is not None and str(planner_origin).strip():
        data["planner_origin"] = str(planner_origin).strip()[:64]
    return {
        "action": ACTION_EXECUTE_MUTATE,
        "trace_id": str(trace_id).strip(),
        "data": data,
    }


def build_action_feedback_body(
    *,
    trace_id: str,
    tool_name: str,
    correlation_id: str,
    stdout: str,
    stderr: str,
    exit_code: int,
    status: str = "ok",
    skipped_reason: str | None = None,
    mutate_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Payload for omni-action-feedback (producer: executor)."""
    return {
        "trace_id": str(trace_id).strip(),
        "tool_name": str(tool_name).strip()[:256],
        "correlation_id": str(correlation_id).strip(),
        "stdout": (stdout or "")[:24000],
        "stderr": (stderr or "")[:12000],
        "exit_code": int(exit_code),
        "status": str(status)[:32],
        "skipped_reason": (skipped_reason or "")[:2000] if skipped_reason else "",
        "mutate_args": dict(mutate_args) if isinstance(mutate_args, dict) else {},
    }


def build_suggest_os_runbook_body(
    trace_id: str,
    *,
    diagnosis: str,
    confidence: float,
    source: str,
    runbook_title: str,
    commands: list[dict[str, Any]],
    reasoning_chain: dict[str, Any] | None = None,
    verification_evidence_digest: str = "",
) -> dict[str, Any]:
    """Inner Kafka JSON for shadow-mode OS command runbook (human execute)."""
    data: dict[str, Any] = {
        "diagnosis": (diagnosis or "").strip()[:16000],
        "confidence": max(0.0, min(1.0, float(confidence))),
        "source": str(source or "SHADOW_OS_PLANNER").strip()[:120],
        "runbook_title": (runbook_title or "Shadow OS Runbook").strip()[:240],
        "commands": commands if isinstance(commands, list) else [],
        "verification_evidence_digest": (verification_evidence_digest or "").strip()[:2000],
    }
    if isinstance(reasoning_chain, dict) and reasoning_chain:
        data["reasoning_chain"] = reasoning_chain
    return {
        "action": ACTION_SUGGEST_OS_RUNBOOK,
        "trace_id": str(trace_id).strip(),
        "data": data,
    }


def infer_exit_code_from_tool_output(text: str) -> int:
    """Heuristic: tool returns human-readable string; 1 if error markers."""
    s = (text or "").lower()
    if "[data] error" in s or "[data] api_error" in s or "[data] stale_state" in s:
        return 1
    if "error" in s[:200] and "diagnosis" in s[:400]:
        return 1
    return 0
