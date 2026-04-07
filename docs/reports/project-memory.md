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
