"""Optional typing for agentic planner JSON (documentation / tests; runtime uses loose dict)."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


class AgenticPlannerJson(TypedDict, total=False):
    """Expected shape for LLM single-round JSON (subset enforced in prompts)."""

    decision: Literal["discovery", "mutate", "done", "escalate"]
    thought: str
    tool_name: str
    args: dict[str, Any]
    tool_args: dict[str, Any]
    phase: Literal["observe", "verify", "remediate", "discovery", "done", "escalate"]
    step: Literal["readonly", "mutate", "done"]
    analysis: str
    working_hypothesis: str
    resolution_summary: str
    proof_lane_hint: str
    missing_preconditions: list[str]
    escalation_reason: str
