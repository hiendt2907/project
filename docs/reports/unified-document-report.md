# Unified Document Report

This file consolidates all current report documents under `docs/reports/` into one place for review.
Original files are kept unchanged.

## Source Files

- `docs/reports/phase-1-state-machine-report.md`
- `docs/reports/phase-2-test-pyramid-report.md`
- `docs/reports/phase-3-e2e-verification-report.md`
- `docs/reports/phase-4-adapterization-report.md`
- `docs/reports/phase-5-slo-gates-report.md`
- `docs/reports/phase-1-report.md`
- `docs/reports/phase-1-review.md`
- `docs/reports/phase-2-report.md`
- `docs/reports/phase-2-review.md`
- `docs/reports/phase-3-report.md`
- `docs/reports/phase-3-review.md`
- `docs/reports/phase-4-report.md`
- `docs/reports/phase-4-review.md`
- `docs/reports/phase-5-report.md`
- `docs/reports/phase-5-review.md`
- `docs/reports/project-memory.md`

---

## phase-1-state-machine-report.md

# Phase 1 State Machine Report

## Objective

Establish a unified autonomy transition contract over existing worker runtime paths and enforce terminal tombstone behavior for non-recoverable branches.

## Delivered

- Added shared transition/tombstone module: `src/workers/autonomy_contract.py`.
- Wired transition markers into stream ingest, evidence reasoning, proactive processing, action execution, and feedback loop.
- Added trace dual propagation support in Kafka transport (`header + payload`).
- Added executor rate-limiting by action fingerprint.

## Security Impact

- Fail-closed terminal path via tombstone + DLQ on non-recoverable branches.
- Added mutation burst protection in executor.
- Preserved backward compatibility by keeping payload `trace_id`.

## Test Evidence

- Unit/integration additions:
  - `tests/integration/test_autonomy_loop_transitions.py`
  - `tests/integration/test_feedback_replan_loop.py`
  - `tests/integration/test_fault_injection_matrix.py`

## Risks / Follow-up

- Full trace parity validation in live Kafka headers still requires strict E2E checks in Phase 3.
- Broader transition coverage for all legacy branches should continue in Phase 2 hardening.

---

## phase-2-test-pyramid-report.md

# Phase 2 Test Pyramid Report

## Scope

Increase confidence in closed-loop autonomy by adding integration tests for transitions, feedback replanning, and fault injection realism.

## Planned Evidence

- Transition ordering + terminal-state assertions.
- Retry/replan escalation assertions.
- Latency and partial failure simulation in integration tests.

## Security Focus

- Ensure deny/rate-limit/timeout paths terminate with auditable tombstone states.

## Status

Planned and partially scaffolded with integration test modules.

---

## phase-3-e2e-verification-report.md

# Phase 3 E2E Verification Report

## Scope

Move from log-grep style checks to machine-verifiable stage assertions by trace.

## Planned Evidence

- Strict stage matrix checks in E2E scripts.
- Trace chain checks across gateway and worker roles.
- Trace parity checks (payload/log chain; header parity where available).

## Status

In progress: script-level strict assertions added, further runtime validation pending cluster run.

---

## phase-4-adapterization-report.md

# Phase 4 Adapterization Report

## Scope

Introduce portability contracts so autonomy loop can run with K8s adapter and external adapters without changing core semantics.

## Delivered / Planned

- Added adapter protocol contracts in `src/workers/adapters/contracts.py`.
- Added K8s-oriented and mock external adapter implementations.

## Status

Initial contract scaffold complete; deeper runtime routing integration remains.

---

## phase-5-slo-gates-report.md

# Phase 5 SLO and Gates Report

## Scope

Define autonomy operational gates and enforce promotion safeguards in CI/lab workflows.

## Delivered / Planned

- Added `make autonomy-gate` target to run key autonomy checks and strict system audit.
- SLO gate documentation added in architecture/runbooks docs.

## Status

Baseline gate target added; threshold tuning and dashboard tie-in remain.

---

## phase-1-report.md

# Phase 1 Report - Mutate Channel Semantics

## Objective

Enforce mutate-only execution semantics for `EXECUTE_MUTATE`.

## Scope

- `src/workers/autonomous_execute.py`
- `src/workers/analyst_agentic_loop.py`
- `src/workers/kafka_actions_consumer.py`
- `src/pkg/reasoning/reason_codes.py`

## Contract Changes

- Split taxonomy into mutate-only and read-only tool sets.
- Read-only tool on mutate channel is rejected with auditable reason code.
- Planner read-only proposal is routed to suggestion path, not execution.

## What Changed in System Behavior

- `EXECUTE_MUTATE` can no longer "succeed" through read/query tools.
- Executor emits explicit deny telemetry for non-mutating tool requests.

## Tests/E2E

- `tests/test_autonomous_contract.py`
- `tests/test_analyst_agentic_loop.py`

## Known Risks

- Legacy callers still sending read-only tools to mutate channel need remediation upstream.

## Memory Applied

- Applied from `docs/reports/project-memory.md` sections: `Invariants`, `ReasonCodes`.

---

## phase-1-review.md

# Phase 1 Review - Mutate Channel Semantics

## Findings

- Channel semantics were previously permissive and allowed read-only tools in mutate flow.
- Planner reject taxonomy needed standard reason codes for SIEM consistency.

## Design Decisions

- Use capability taxonomy as source of truth; keep prefix policy as advisory evolution path.
- Fail-closed on executor side even if planner misbehaves.

## Trade-offs

- Stricter mutate allowlist may reduce short-term flexibility but removes false execution closure.

---

## phase-2-report.md

# Phase 2 Report - Classifier & Matrix Refactor

## Objective

Move classification to label-first matching with deterministic priority.

## Scope

- `src/workers/diagnostic_mapping.py`
- `config/diagnostic_matrix.yaml`
- `tests/test_diagnostic_mapping.py`

## Contract Changes

- Matrix rows now support priority and label predicates (`alertname`, `domain`, `reason`, `workload`).
- Regex remains fallback only when label predicates are absent.

## What Changed in System Behavior

- `ProbeFailureLab` classification now resolves via labels and avoids broad regex capture.
- Deterministic row ordering reduces generic-row misroutes.

## Tests/E2E

- `tests/test_diagnostic_mapping.py`
- `scripts/validate_classifier_regression_gate.py`

## Known Risks

- Alerts without structured labels still depend on regex fallback quality.

## Memory Applied

- Applied from `docs/reports/project-memory.md`: `FailurePatterns`, `CrossPhaseConstraints`.

---

## phase-2-review.md

# Phase 2 Review - Classifier & Matrix Refactor

## Findings

- Over-broad regex rows are the main source of incident-group drift.
- Priority ordering is required to keep specific rows ahead of generic catch-all.

## Design Decisions

- Label predicates are strict and must all match when configured on a row.
- Sorting by `priority` is explicit instead of relying on file order alone.

## Trade-offs

- Requires more disciplined alert labeling upstream to unlock full classifier precision.

---

## phase-3-report.md

# Phase 3 Report - Planner Conformance & Proof Gate

## Objective

Enforce planner conformance and gate every mutate action with Proof of Fault + 3-sigma windowing.

## Scope

- `src/workers/evidence_consumer.py`
- `src/workers/settings.py`
- `tests/test_evidence_proof_gate.py`

## Contract Changes

- Added proof gate: critical physical evidence + sigma gate (`dr` or z-score threshold) + observation window.
- Added reason-code path for planner/channel rejection and proof-block decisions.

## What Changed in System Behavior

- Mutate is blocked when fault evidence is weak or sigma/window criteria are not met.
- Planner read-only/hallucinated suggestions now produce auditable non-mutate outcomes.

## Tests/E2E

- `tests/test_evidence_proof_gate.py`
- `tests/test_analyst_agentic_loop.py`

## Known Risks

- If baseline snapshot is stale, mutate may be blocked conservatively.

## Memory Applied

- Applied from `docs/reports/project-memory.md`: `Invariants`, `Guardrails`, `ReasonCodes`.

---

## phase-3-review.md

# Phase 3 Review - Planner Conformance & Proof Gate

## Findings

- Planner validity requires both schema checks and tool-capability checks.
- Evidence-gate controls must sit after planning and before emission to prevent unsafe mutate.

## Design Decisions

- Keep proof gate centralized in evidence-consumer path before `emit_execute_mutate`.
- Keep observation window configurable via `OMNI_AUTONOMOUS_SIGMA_OBSERVATION_WINDOW`.

## Trade-offs

- Fail-closed gating can reduce automation in borderline incidents but lowers false-positive mutation risk.

---

## phase-4-report.md

# Phase 4 Report - Contract Tests & Matrix Coverage

## Objective

Expand contract tests for mutate semantics, classifier priority, and proof-gate windowing.

## Scope

- `tests/test_autonomous_contract.py`
- `tests/test_diagnostic_mapping.py`
- `tests/test_evidence_proof_gate.py`
- `tests/test_analyst_agentic_loop.py`

## Contract Changes

- Added read-only rejection test on mutate channel.
- Added label-first classifier and priority tests.
- Added proof-gate tests for no-proof, sigma-block, and observation-window pass.

## What Changed in System Behavior

- Regression surfaces are now checked earlier in unit tests before runtime verification.

## Tests/E2E

- Unit suite focused on mutate semantics and classifier/proof contracts.
- Incident matrix JSON reporting remains in `reports/incident-matrix/latest.json`.

## Known Risks

- Integration coverage still depends on cluster health for full confidence.

## Memory Applied

- Applied from `docs/reports/project-memory.md`: `CrossPhaseConstraints`, `FailurePatterns`.

---

## phase-4-review.md

# Phase 4 Review - Contract Tests & Matrix Coverage

## Findings

- Test contracts now directly encode the most frequent regressions (misroute + unsafe mutate).
- Proof-gate behavior is deterministic with explicit reason codes.

## Design Decisions

- Add narrowly-scoped tests instead of broad integration-heavy suites for faster iteration.

## Trade-offs

- Unit confidence increased, but runtime confidence still requires build/rollout/e2e cycle.

---

## phase-5-report.md

# Phase 5 Report - Gates & Release Hardening

## Objective

Promote mutate semantics, classifier regression, and documentation completeness into enforced gates.

## Scope

- `scripts/validate_mutate_only_gate.py`
- `scripts/validate_classifier_regression_gate.py`
- `scripts/validate_phase_docs_gate.py`
- `Makefile`
- `.github/workflows/ci.yml`

## Contract Changes

- CI/Make now fail when mutate/read-only taxonomy regresses.
- CI/Make now fail when `ProbeFailureLab` classifier mapping regresses.
- CI/Make now fail when required phase docs/project memory are missing.

## What Changed in System Behavior

- Architecture constraints are now enforceable, not advisory.
- Release path blocks incomplete documentation/memory assimilation.

## Tests/E2E

- Gate scripts are executable in local and CI workflows.
- `autonomy-gate` includes new contract tests and static gates.

## Known Risks

- Gate scripts assume local import path `src/`; CI/local parity must be preserved.

## Memory Applied

- Applied from `docs/reports/project-memory.md`: `Guardrails`, `CrossPhaseConstraints`.

---

## phase-5-review.md

# Phase 5 Review - Gates & Release Hardening

## Findings

- Static gates are effective to guard critical semantics before cluster rollout.
- Documentation gate prevents memory loss across phases.

## Design Decisions

- Keep gate logic simple and deterministic (no network dependency).
- Pair static gates with targeted pytest in CI for behavior contracts.

## Trade-offs

- More strict CI can increase initial friction but reduces architectural drift.

---

## project-memory.md

# Project Memory Registry

## Invariants

- `EXECUTE_MUTATE` only executes mutate-capable tools; read/query tools must route to `SUGGEST_REMEDIATION`.
- Mutate decisions are fail-closed in `prod` and must keep `trace_id` + auditable `reason_code`.
- Planner output cannot override Proof-of-Fault controls (critical evidence + 3-sigma + observation window).

## FailurePatterns

- Classifier misroute can happen when broad regex rows run before label-constrained rows.
- Planner can emit read-only/hallucinated tools even when JSON shape is valid.
- Single metric spikes are noisy; windowed sigma checks are required before mutation.

## ReasonCodes

- Semantic/channel: `ERR_SEM_CHANNEL_MISMATCH`, `ERR_SEM_INVALID_TOOL_TAXONOMY`.
- Governance: `ERR_GOV_NS_OUT_OF_BOUNDS`, `ERR_GOV_UNAUTHORIZED_MUTATION`, `ERR_GOV_ENV_PROD_STRICT`.
- Reasoning/evidence: `ERR_REA_NO_PHYSICAL_PROOF`, `ERR_REA_SIGMA_GATE_BLOCKED`, `ERR_REA_SCHEMA_VIOLATION`, `ERR_REA_HALLUCINATION_DETECTED`.
- Terminal: `SUCCESS_VERIFIED_EVIDENCE`, `ESC_TIMEOUT_TOMBSTONE`, `ESC_MAX_ATTEMPTS_EXCEEDED`.

## Guardrails

- Keep mutate/read-only taxonomy explicit in runtime constants and CI gates.
- Keep classifier regression gate for `ProbeFailureLab` not mapping to `ollama_500_context`.
- Documentation gate blocks incomplete phase records.

## CrossPhaseConstraints

- Any change touching mutate/classifier/planner must update tests and gates together.
- Every phase report must include `What Changed in System Behavior` and `Memory Applied`.

