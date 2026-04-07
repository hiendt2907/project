# North-Star Spec (Safety-First Autonomy)

## Purpose
Define the final system shape and non-negotiable invariants for Omni autonomy, so implementation is guided by architecture outcomes instead of isolated bug fixes.

## Product Target
- Phase 1 (3-6 months): autonomous K8s SRE operator with closed-loop remediation.
- Phase 2 (6-12 months): same core loop exported to multi-domain adapters.

## Closed-Loop Contract
For each `trace_id`, the system must execute:
1. ingest normalized signal,
2. verify against live state machine evidence,
3. produce bounded plan with RAG + LLM,
4. execute under policy constraints,
5. verify outcome,
6. write back learning,
7. end in terminal state with auditable reason.

## Non-Negotiable Invariants
- `trace_id` is unified across ingress, planner, executor, verifier, and audit.
- Every terminal branch emits a canonical tombstone and audit transition.
- No mutate action bypasses governance checks in `prod`.
- Planner output cannot execute outside schema + allowlist + role scope.
- Learning write-back only occurs after successful verification.

## Environment Contract
- `OMNI_ENV_MODE=prod|dev`, default `prod`.
- `prod`:
  - fail-closed governance,
  - bounded namespace/role blast radius,
  - explicit deny + audit reasons,
  - promotion gates required.
- `dev`:
  - high-action mode per role capability,
  - no silent bypass of trace/audit/idempotency,
  - still emits full transition + feedback evidence.

## Control Planes
- Ingest plane: normalize and route incidents by trace.
- Diagnostic plane: SDK truth/evidence collection.
- Reasoning plane: RAG-first, LLM planner with schema discipline.
- Execution plane: role-scoped mutators under policy.
- Verification plane: post-action state and confidence checks.
- Learning plane: experience/upsert after verified success.
- Governance plane: environment policy, blast radius, approval mode.
- Audit plane: immutable transition/event evidence for release gates.

## Release Criteria
- Governance: `prod` contract enforced at runtime and in CI gate.
- Contract: deterministic transition ordering + terminal proof in tests.
- Quality: planner/schema conformance and replay/idempotency checks.
- Runtime: strict audit + selected E2E matrix pass with trace parity.
