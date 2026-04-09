"""Omni-actions contract: SUGGEST_REMEDIATION (English diagnosis JSON for executor audit)."""

from __future__ import annotations

from typing import Any

ACTION_SUGGEST_REMEDIATION = "SUGGEST_REMEDIATION"


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
