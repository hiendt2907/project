# Incident Report: Omni Worker Connectivity & RAG Migration Failure

## Summary
On 2026-03-31, the `omni-worker` service experienced a persistent `CrashLoopBackOff` following a migration of the RAG data layer from Qdrant to PostgreSQL (CNPG). The service was unable to authenticate with the database and encountered multiple code/environment mismatches. Fixes were applied via environment overrides and ConfigMap-based code patching.

## Timeline of Errors & Resolution

| Source | Error Description | Root Cause | Fix |
| :--- | :--- | :--- | :--- |
| **Authentication** | `FATAL: password authentication failed for user "appuser"` | Default DSN in source code used 'password' instead of the Kubernetes secret value. | Injected correct `POSTGRES_RAG_DSN` into Deployment. |
| **Database** | `FATAL: database "omnidb" does not exist` | Code defaulted to `omnidb` while CNPG cluster initialized with `ragdb`. | Corrected DSN to point to `ragdb`. |
| **Code Mismatch** | `AttributeError: ... has no attribute 'qdrant'` | The Docker image (`v6.4-chaos`) still contained legacy Qdrant code while the worker context was updated to use PGVector. | Patched pod using ConfigMap volume mounts for source files. |
| **Dependencies** | `ModuleNotFoundError: No module named 'pgvector'` | The Python package `pgvector` was not installed in the base image. | Updated Deployment command to run `pip install pgvector` before startup. |
| **Permissions** | `error: must be owner of table doc_sop` | Tables were created by `postgres` superuser during bootstrap, but worker uses `appuser`. | Executed `ALTER TABLE ... OWNER TO appuser` via superuser. |

## Root Cause Analysis (RCA)
1.  **Fragmentation of Configuration**: Credentials existed in K8s Secrets, but the application fallback defaults were stale and incorrect.
2.  **Image Stale/Desync**: Development changes in the workspace were not reflected in the deployed image (`v6.4-chaos`), leading to runtime attribute errors.
3.  **Missing Bootstrap Automation**: The application lacked a robust "Initial Setup" routine to create tables and verify permissions automatically on first run.

## Recommendations for Prevention
1.  **Unified Configuration**: Use a central `ConfigMap`/`Secret` as the ONLY source of truth for DSNs. Remove hardcoded defaults from code.
2.  **Startup Health Checks**: Implement a `pre-start` script that verifies:
    *   DB connectivity.
    *   Necessary extensions (`vector`).
    *   Table ownership.
3.  **CI/CD Synchronization**: Ensure deployment manifests and images are built/pushed together to avoid "Stale Image" bugs.
4.  **Schema Auto-Management**: Integrate `Alembic` or a built-in `ensure_ready()` call that runs on every deployment.

---

# Design: Autonomous Self-Healing System (Auto-Fix)

To prevent "mò" (trial-and-error manual debugging) in the future, we will implement an **Omni Self-Healing Layer**.

### Phase 1: Log Observation & Trigger
*   **System**: A "SRE-Agent" pod (privileged) monitors container logs.
*   **Trigger**: Detecting specific regex patterns:
    *   `ModuleNotFoundError: No module named '(.*)'`
    *   `FATAL: password authentication failed`
    *   `database "(.*)" does not exist`

### Phase 2: Actionable Knowledge Base (AKB)
The system will search its RAG (PostgreSQL) for "How to fix [Error Pattern]".
*   **Missing Module**: Trigger `pip install` or update `requirements.txt` and signal a rebuild.
*   **Auth Failure**: Compare the DSN in environment vs. the Secret `omni-postgres-app`. If desynced, update Deployment env vars.
*   **Permission Error**: Identify current user (`whoami`) and target table owner. Run a `GRANT` or `ALTER OWNER` job.

### Phase 3: Autonomous Remediation (HIL - Human In Loop)
*   **Level 1**: Suggest the fix via Telegram + specific "Approve Fix" button.
*   **Level 2**: Auto-apply fix if in "Full Authority" mode (e.g., auto-patching ConfigMap or running SQL).

---

> [!IMPORTANT]
> The next step is to implement the **Startup Bootstrap Agent** within the project to handle schema and permission checks automatically.
