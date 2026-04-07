# Autonomy State Machine

## Canonical Transitions

`INGESTED -> CONTEXT_READY -> DIAGNOSED -> PLAN_EMITTED -> EXECUTED -> VERIFIED_SUCCESS | REQUIRES_HUMAN`

## Notes

- Every trace must end with exactly one terminal state.
- Non-recoverable failures must emit tombstone + terminal escalation.
- Transition evidence is emitted to audit streams with ordered sequence per trace.