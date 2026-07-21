# ADR-006: Evidence/Command Contract Convergence — Findings

**Date:** 2026-07-21
**Status:** Accepted — honest close-out of the Phase 0–6 roadmap's Phase 6

## Context

The Phase 0–6 roadmap (`docs/handoffs/PHASE_0_6_PROGRESS.md`) started from a live
audit that found Omni's K8s tool-calling lane and VM/AOIP recovery lane use
structurally different, undocumented wire shapes: **3 Evidence shapes** and
**6 Command/CommandResult shapes**. Phase 0b defined a canonical
`src/pkg/contracts/` module (`CanonicalEvidence`, `CorrelationIdentity`) and proved
both lanes' Evidence shapes convert into it losslessly. Phase 6's job is to
re-run the same grep-based audit used to size the original plan and report the
real, current state — not the intended one.

## Fresh audit (2026-07-21, same method as the original)

**Evidence shapes — still 3, unchanged, at their original call sites:**

| Shape | Location | Status |
|---|---|---|
| `DiagnosticEvidenceDict` | `src/pkg/reasoning/schema.py:13` | unchanged |
| `EvidenceItem` / `AgentEvidenceRequest` | `src/gateway/routes/agent_webhook.py:78,102` | unchanged |
| `EvidenceObject` | `src/workers/diagnostic_evidence.py:16` | unchanged |
| `CanonicalEvidence` (Phase 0b) | `src/pkg/contracts/evidence.py:27` | exists, proven lossless, **zero production import sites** — only `tests/test_pkg_contracts.py` imports it |

**Command/CommandResult shapes — still 6, unchanged:**

`ToolCallPayload` (`workers/tools.py:74`), `CommandItem`/`CommandResultItem`
(`gateway/routes/agent_commands.py:106,120`), `EnqueueRuntimeCommand`
(`gateway/routes/agent_runtime.py:242`), `CommandRecord` (`aoip/agent/delivery.py:51`),
`RecoveryRequest`/`decode_recovery_command` (`aoip/recovery.py:160`,
`aoip/agent/operations.py:341`), `issue_capability_command`
(`aoip/capabilities/systemd_restart.py:168`) — all present, all unchanged.
`CorrelationIdentity` (Phase 0b's shared-identity-fields extraction) has the same
zero-production-adoption status as `CanonicalEvidence`.

**Conclusion: wire-shape unification did not happen.** The canonical contracts
module was deliberately scoped in Phase 0b as additive and non-migrating ("freeze
the contract and prove it's lossless" — see that phase's own verification notes) —
migrating every call site was explicitly deferred, and nothing in Phases 1–5
routed through it. This is not a regression or an oversight surfacing late; it is
the original Phase 0b scope decision, now confirmed still true five phases later.

## What actually converged (real, tested, live-verified across Phases 2, 4, 5)

Wire shapes stayed separate, but the **governance/decision layer** the roadmap
cared about most did converge into one shared model:

- **Tier gate**: `pkg.autonomy.tier_gate::resolve_tier()` /
  `gate_decision_for_tool()` now gates both the K8s lane (pre-existing) and the
  VM recovery lane (`agent_runtime.py::_enforce_mutation_toggle` →
  `_enforce_tier_gate`, Phase 2) — one shadow/assist/auto decision authority, not
  two. Live-verified: a `shadow`-tier tenant's VM recovery command is blocked
  (423), an `assist`/`auto`-tier tenant's LOW-risk command is allowed — same
  matrix the K8s lane already used.
- **Risk taxonomy**: `pkg/risk_taxonomy.py` carries both lanes' capabilities in
  one table (`k8s_rollout_restart: LOW` alongside `systemd.restart_unit: LOW`,
  added Phase 2).
- **Closed-loop dispatch pattern**: Phase 4 gave the VM lane the same
  diagnosis→confidence-gated→auto-dispatch shape the K8s lane's tool-calling
  loop already had (`services/analyst/diagnosis_loop.py`'s `suggested_recovery`
  → `workers/auto_recovery_bridge.py` → gateway enqueue), even though the
  concrete payload dataclass differs from `ToolCallPayload`.
- **Multi-tenant isolation, live-verified concurrently (Phase 5)**: idempotency
  (`command_identity()`, tenant is part of the hash) and the command/audit
  layer's `canonical_scope` (`"{tenant}:svc:{unit}"`) are both tenant-scoped
  correctly — confirmed via two tenants recovering distinct real incidents at
  the same time with zero cross-tenant leakage in the captured Redis records.
  One residual, non-exploitable gap was found and documented there: the VM-side
  `ExecutionLease` lock key (`svc:{unit}`) is not tenant-namespaced (fail-safe,
  not fail-open — see Phase 5's ledger entry for the live Redis-level proof).
- **Audit ledger (CRAT)**: both lanes already wrote through the same
  `src/services/audit_ledger/` hash-chain — this convergence pre-dates this
  roadmap and was not touched by it.

## Decision

Do not force wire-shape migration retroactively to make this ADR's before/after
numbers look better. The governance-layer convergence (tier, risk, audit,
closed-loop dispatch pattern) is what actually made the two lanes "one system"
in the sense that mattered for this roadmap — a tenant's autonomy tier, risk
posture, and audit trail are now uniform regardless of which lane a mutation
came through. The wire-shape convergence remains available as future work
(`src/pkg/contracts/` is proven lossless and ready to adopt) but is not free —
migrating `agent_webhook.py`, `diagnostic_evidence.py`, and the six Command
shapes onto canonical types touches every evidence/command call site in both
lanes, a materially larger and riskier change than anything in this roadmap.
Revisit only if a concrete pain point (a real bug caused by shape drift, or a
new lane that needs to speak both dialects) makes the cost worth paying.

## Verification

```
grep -c "class DiagnosticEvidenceDict\|class EvidenceItem\|class AgentEvidenceRequest\|class EvidenceObject" \
  src/pkg/reasoning/schema.py src/gateway/routes/agent_webhook.py src/workers/diagnostic_evidence.py
# 1, 2, 1  → 3 shapes, unchanged from the original audit

grep -rln "pkg.contracts.evidence\|CorrelationIdentity" src/ tests/ --include=*.py | grep -v __pycache__
# tests/test_pkg_contracts.py, src/pkg/contracts/* only — zero production adoption
```
