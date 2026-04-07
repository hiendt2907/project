# Phase Report Template

## Metadata
- Phase: `Phase <N> - <Title>`
- Date range: `<YYYY-MM-DD> -> <YYYY-MM-DD>`
- Owner(s): `<team/person>`
- Status: `draft | in_progress | complete`
- Related plan: `<link/path>`

## 1) Objective and Scope
- Objective:
- In scope:
- Out of scope:
- Success criteria:

## 2) Code Paths Touched
- Primary runtime paths:
  - `<path>::<function_or_module>`
  - `<path>::<function_or_module>`
- Supporting paths:
  - `<path>`

## 3) Design and Logic Changes
- Existing behavior (before):
- Implemented behavior (after):
- Why this change was needed:
- Backward compatibility impact:

## 4) Security Impact
- Threats addressed:
- New/updated guardrails:
- Fail-closed behavior:
- Residual risks:

## 5) Testing Evidence
### Unit tests
- Added/updated:
- Command:
- Result:

### Integration tests
- Added/updated:
- Command:
- Result:

### E2E verification
- Scenario matrix:
- Command(s):
- Trace evidence:
- Result:

## 6) Operational Evidence
- Metrics/SLO deltas:
- Audit outcomes:
- DLQ/timeout/escalation summary:
- Notable logs (short excerpts only):

## 7) Issues and Mitigations
- Issue:
  - Symptom:
  - Mitigation:
  - Current state:

## 8) Documentation Updates
- Updated docs:
  - `<path>`
- Runbook updates:
  - `<path>`
- Knownbase update:
  - `docs/vendor/knownbase.md`: `yes | no` (if yes, summarize entry)

## 9) Release Readiness
- Gate checklist:
  - [ ] Transition coverage complete
  - [ ] Security tests pass
  - [ ] E2E strict checks pass
  - [ ] Terminal outcomes verified
  - [ ] Docs/report complete
- Go/No-Go:
- Rollback plan:

## 10) Next Actions
- Immediate follow-ups:
- Deferred items:
- Owner and ETA:
