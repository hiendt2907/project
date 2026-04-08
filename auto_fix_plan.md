# Implementation Plan: Autonomous Self-Healing Layer

> **Doc map:** [docs/DOCUMENTATION_INDEX.md](docs/DOCUMENTATION_INDEX.md) · **Canonical:** [docs/vendor/OMNI_PROJECT_CANONICAL.md](docs/vendor/OMNI_PROJECT_CANONICAL.md). Planning snapshot — not necessarily current runtime.

This plan outlines the creation of an autonomous system capable of detecting, diagnosing, and repairing common infrastructure and application errors (like authentication failures, missing packages, and DB schema mismatches) without manual intervention.

## User Review Required

> [!IMPORTANT]
> This system will have the authority to run modifying commands (e.g., `pip install`, `psql ALTER`) inside the pods. We must define a security boundary for what it can and cannot do without explicit approval via Telegram.

## Proposed Changes

We will introduce a new module `src/sre` (Site Reliability Engineering) and integrate it into the `omni-worker` startup sequence.

### Phase 1: Diagnostic Foundation

#### [NEW] [self_healer.py](file:///Users/hiendang/project/src/sre/self_healer.py)
*   Define `DiagnosticMatcher`: A registry of error patterns (regex) mapping to remediation actions.
*   Implement `SelfHealer.diagnose(tail_log: str)`: Analyzes recent logs to identify the root cause.

#### [NEW] [remediations.py](file:///Users/hiendang/project/src/sre/remediations.py)
*   Implement standard fix functions:
    *   `fix_missing_dependency(pkg_name)`: Attempts `pip install` and updates `requirements.txt`.
    *   `fix_auth_failure(dsn_env)`: Checks K8s secrets and updates deployment if missmatched.
    *   `fix_db_permissions(table)`: Executes superuser SQL to correct ownership.

### Phase 2: Integration into Startup

#### [MODIFY] [omni_worker.py](file:///Users/hiendang/project/src/workers/omni_worker.py)
*   Wrap the `build_context` and initial loops in a `try-except` block.
*   On failure, trigger `SelfHealer` before giving up and crashing.
*   If a fix is applied, the worker will attempt a "soft restart" of its internal loops.

#### [MODIFY] [omni-worker.yaml](file:///Users/hiendang/project/deployments/omni-worker.yaml)
*   Add a sidecar container or elevated service account permissions (if needed) to allow the worker to patch its own deployment or run superuser SQL via a temporary job.

### Phase 3: Human-in-the-Loop (HIL)

#### [MODIFY] [handlers.py](file:///Users/hiendang/project/src/workers/handlers.py)
*   Integrate detection into the main message handler.
*   On system error, send a Telegram alert with a:
    *   **Diagnosis**: "Missing `pgvector` library."
    *   **Proposed Fix**: "Run `pip install pgvector`."
    *   **Action Button**: `[✅ Apply Hotfix]`

## Open Questions
*   **Permissions**: Should the worker have `kubectl` access within the cluster to patch itself, or should we use a separate "SRE-Operator"?
*   **Persistence**: Should hotfixes applied via `pip install` be persisted to a new image automatically?

## Verification Plan

### Automated Tests
*   Simulate a `ModuleNotFoundError` by renaming a library and verify the `SelfHealer` detects and re-installs it.
*   Simulate a DB connection failure and verify the system checks the `POSTGRES_RAG_DSN`.

### Manual Verification
*   Intentionally break the `omni-worker` (e.g., change its password in environment) and monitor the Telegram bot for the auto-remediation prompt.
