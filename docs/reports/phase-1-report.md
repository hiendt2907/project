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

## Iteration Update - Security Cleanup (Strict)
### Objective
Remove hardcoded secret-like defaults and enforce fail-fast secret hygiene.

### Scope
- `src/workers/settings.py`
- `src/rag/pgvector_store.py`
- `src/training/cli_hil_ingest.py`
- `k8s/monitor/grafana.yaml`
- `k8s/monitor/grafana-telegram-alerting-secret.yaml`
- `.gitleaks.toml`
- `.pre-commit-config.yaml`

### What Changed in System Behavior
- Runtime DSN defaults now use `${OMNI_DB_PASSWORD}` placeholder instead of embedded password.
- `PostgresRAGSettings` now fails fast when placeholder/default-style DSN is used at runtime.
- Secret scan is now mandatory in CI/local gate via `gitleaks` (critical fail for new leaks).
