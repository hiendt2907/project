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
) -> dict[str, Any]:
    """Inner Kafka JSON body (wrapped by producer as envelope ``data`` string)."""
    return {
        "action": ACTION_SUGGEST_REMEDIATION,
        "trace_id": trace_id,
        "data": {
            "diagnosis": (diagnosis or "").strip()[:16000],
            "confidence": float(confidence),
            "source": source,
            "suggested_tool": (suggested_tool or "").strip()[:256],
        },
    }
