# Phase 3 Review - Planner Conformance & Proof Gate

## Findings
- Planner validity requires both schema checks and tool-capability checks.
- Evidence-gate controls must sit after planning and before emission to prevent unsafe mutate.

## Design Decisions
- Keep proof gate centralized in evidence-consumer path before `emit_execute_mutate`.
- Keep observation window configurable via `OMNI_AUTONOMOUS_SIGMA_OBSERVATION_WINDOW`.

## Trade-offs
- Fail-closed gating can reduce automation in borderline incidents but lowers false-positive mutation risk.
