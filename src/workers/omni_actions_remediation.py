"""Omni-actions contracts: remediation suggestion + Shadow OS runbook suggestion."""

from __future__ import annotations

from typing import Any

ACTION_SUGGEST_REMEDIATION = "SUGGEST_REMEDIATION"
ACTION_SUGGEST_OS_RUNBOOK = "SUGGEST_OS_RUNBOOK"


def build_suggest_remediation_body(
    trace_id: str,
    *,
    diagnosis: str,
    confidence: float,
    source: str,
    suggested_tool: str,
    verdict: str | None = None,
    lane: str | None = None,
    thought_process: list[str] | None = None,
    invariant_id: str | None = None,
    reasoning_chain: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Inner Kafka JSON body (wrapped by producer as envelope ``data`` string)."""
    data: dict[str, Any] = {
        "diagnosis": (diagnosis or "").strip()[:16000],
        "confidence": float(confidence),
        "source": source,
        "suggested_tool": (suggested_tool or "").strip()[:256],
    }
    if reasoning_chain is not None and isinstance(reasoning_chain, dict):
        data["reasoning_chain"] = reasoning_chain
    else:
        rc: dict[str, Any] = {}
        if verdict:
            rc["verdict"] = str(verdict)[:64]
        if lane:
            rc["lane"] = str(lane)[:32]
        if thought_process:
            rc["thought_process"] = [str(x)[:2000] for x in thought_process][:32]
        if invariant_id:
            rc["invariant_id"] = str(invariant_id)[:128]
        if rc:
            data["reasoning_chain"] = rc
    return {
        "action": ACTION_SUGGEST_REMEDIATION,
        "trace_id": trace_id,
        "data": data,
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
    data: dict[str, Any] = {
        "diagnosis": (diagnosis or "").strip()[:16000],
        "confidence": float(confidence),
        "source": str(source or "SHADOW_OS_PLANNER").strip()[:120],
        "runbook_title": (runbook_title or "Shadow OS Runbook").strip()[:240],
        "commands": list(commands or [])[:24],
        "verification_evidence_digest": (verification_evidence_digest or "").strip()[:2000],
    }
    if isinstance(reasoning_chain, dict) and reasoning_chain:
        data["reasoning_chain"] = reasoning_chain
    return {
        "action": ACTION_SUGGEST_OS_RUNBOOK,
        "trace_id": trace_id,
        "data": data,
    }
