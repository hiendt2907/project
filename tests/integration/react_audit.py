"""Glassbox audit trail for integration E2E (ReAct planner + simulated cluster)."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any


def _digest_user_prompt(user_content: str) -> dict[str, Any]:
    block = user_content
    if "<TRACE_MEMORY>" in user_content:
        i = user_content.find("<TRACE_MEMORY>")
        j = user_content.find("</TRACE_MEMORY>")
        if j > i:
            block = user_content[i : j + len("</TRACE_MEMORY>")]
    h = hashlib.sha256(block.encode("utf-8", errors="replace")).hexdigest()
    return {
        "user_prompt_sha256_full": hashlib.sha256(user_content.encode("utf-8", errors="replace")).hexdigest(),
        "trace_memory_block_sha256": h,
    }


@dataclass
class ReActAuditTrail:
    """Structured steps for CI / human audit (not blackbox-only asserts)."""

    steps: list[dict[str, Any]] = field(default_factory=list)

    def record_llm(
        self,
        *,
        invocation_id: str,
        round_index: int,
        user_content: str,
        llm_raw: str,
        decision_rule: str,
        parsed_summary: str | None = None,
    ) -> None:
        d = _digest_user_prompt(user_content)
        self.steps.append(
            {
                "kind": "llm_round",
                "invocation_id": invocation_id,
                "round_index": round_index,
                "decision_rule": decision_rule,
                "llm_response_raw": llm_raw[:8000],
                "parsed_summary": parsed_summary,
                **d,
            }
        )

    def record_readonly(
        self,
        *,
        invocation_id: str,
        tool_name: str,
        args: dict[str, Any],
        observation: str,
        simulator_state: dict[str, Any],
    ) -> None:
        self.steps.append(
            {
                "kind": "readonly_executed",
                "invocation_id": invocation_id,
                "tool_name": tool_name,
                "args": args,
                "observation_excerpt": (observation or "")[:4000],
                "simulator_state": dict(simulator_state),
            }
        )

    def record_plan_out(self, *, invocation_id: str, plan: dict[str, Any] | None) -> None:
        self.steps.append(
            {
                "kind": "plan_result",
                "invocation_id": invocation_id,
                "plan": json.loads(json.dumps(plan, default=str)) if plan else None,
            }
        )

    def record_executor_sim(self, *, invocation_id: str, tool_name: str, detail: str) -> None:
        self.steps.append(
            {
                "kind": "executor_simulated",
                "invocation_id": invocation_id,
                "tool_name": tool_name,
                "detail": detail[:2000],
            }
        )

    def maybe_write_json(self) -> None:
        path = (os.environ.get("OMNI_E2E_AUDIT_JSON") or "").strip()
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.steps, f, ensure_ascii=False, indent=2)


def assert_audit_sequence_kinds(trail: ReActAuditTrail, expected_subsequence: list[str]) -> None:
    """Assert ordered kinds appear in order (allowing extra steps)."""
    kinds = [s.get("kind") for s in trail.steps]
    idx = 0
    for want in expected_subsequence:
        try:
            j = kinds.index(want, idx)
        except ValueError as e:
            raise AssertionError(
                f"audit missing kind={want!r} after index {idx}; have={kinds!r}"
            ) from e
        idx = j + 1
