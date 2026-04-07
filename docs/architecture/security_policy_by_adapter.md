# Security Policy by Adapter

## Common Core Policy

- allowlist execution only
- scope guard (namespace/resource constraints)
- lease/freeze conflict handling
- timeout + fail-closed escalation

## Environment Mode Contract

- `OMNI_ENV_MODE=prod|dev` (default `prod`) is global and mandatory.
- `prod`:
  - fail-closed by default,
  - explicit deny reason required,
  - bounded namespace/resource scope,
  - approval/escalation path active.
- `dev`:
  - high-action execution by role capability is allowed,
  - trace/audit/idempotency still mandatory,
  - no silent bypass (all decisions still emitted to audit).

## Kubernetes Adapter

- mutation allowlist for safe `k8s_*` and audited `kubectl_cluster`
- rate-limit by action fingerprint and resource reference
- namespace guard in `prod` for mutating execution path

## External Adapters

- must declare capability profile
- must implement equivalent deny/escalate semantics
- must expose environment-aware behavior parity (`prod`/`dev`)