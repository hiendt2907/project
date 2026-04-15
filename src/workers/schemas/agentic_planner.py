"""Typed contracts for agentic planner JSON and Shadow OS runbook output."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, ValidationError, field_validator


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


class OSCommandItem(BaseModel):
    """One executable Shadow OS command step with safety pair."""

    purpose: str = Field(min_length=3, max_length=400)
    dry_run_command: str = Field(min_length=2, max_length=4000)
    command: str = Field(min_length=2, max_length=4000)
    target: str = Field(min_length=1, max_length=400)
    risk_level: Literal["low", "medium", "high"] = "medium"
    expected_output: str = Field(min_length=1, max_length=2000)
    rollback_command: str = Field(min_length=2, max_length=4000)
    timeout_sec: int = Field(default=60, ge=1, le=1800)
    evidence_refs: list[str] = Field(default_factory=list, min_length=1, max_length=32)
    escalation_required: bool = False

    @field_validator("evidence_refs")
    @classmethod
    def _validate_evidence_refs(cls, refs: list[str]) -> list[str]:
        norm = [str(x).strip() for x in refs if str(x).strip()]
        if not norm:
            raise ValueError("evidence_refs must contain at least one non-empty evidence id")
        return norm[:32]


class SuggestOSRunbookData(BaseModel):
    """Inner data payload for SUGGEST_OS_RUNBOOK."""

    diagnosis: str = Field(min_length=1, max_length=16000)
    confidence: float = Field(ge=0.0, le=1.0)
    source: str = Field(min_length=1, max_length=120)
    runbook_title: str = Field(min_length=1, max_length=240)
    commands: list[OSCommandItem] = Field(default_factory=list, min_length=1, max_length=24)
    reasoning_chain: dict[str, Any] = Field(default_factory=dict)
    verification_evidence_digest: str = Field(default="", max_length=2000)


def validate_suggest_os_runbook_data(payload: dict[str, Any]) -> SuggestOSRunbookData:
    """Validate Shadow OS runbook payload with strict required fields."""

    try:
        return SuggestOSRunbookData.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"invalid SUGGEST_OS_RUNBOOK payload: {exc}") from exc
