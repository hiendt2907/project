"""Pydantic models for the Playbook Engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PlaybookStep:
    step_order: int
    action_type: str          # maps to K8S_SDK_MUTATING_TOOL_NAMES
    target: str               # e.g. deployment name or namespace
    params: dict[str, Any]    # arbitrary args for the action
    timeout_sec: int
    requires_hitl: bool


@dataclass(frozen=True)
class Playbook:
    playbook_id: str
    version: str
    name: str
    severity_filter: str      # "critical", "warning", "info", or "" for all
    approved_by: str
    steps: tuple[PlaybookStep, ...]
    siem_categories: tuple[str, ...] = field(default_factory=tuple)

    # Helper: step lookup by order (1-indexed)
    def step(self, order: int) -> PlaybookStep | None:
        for s in self.steps:
            if s.step_order == order:
                return s
        return None

    def first_step(self) -> PlaybookStep | None:
        if not self.steps:
            return None
        return min(self.steps, key=lambda s: s.step_order)
