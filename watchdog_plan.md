# Implementation Plan: Omni Watchdog (Autonomous SRE Pod)

This plan outlines the creation of a dedicated monitoring and self-healing service (`omni-watchdog`) that operates independently of the main worker to ensure the stability of the RAG data layer and the core application.

## User Review Required

> [!IMPORTANT]
> The Watchdog will require "Write" permissions on Kubernetes Deployments and Jobs to perform restarts and trigger rebuilds. We must ensure this does not violate security policies.

## Proposed Changes

### Phase 1: Core Watchdog Logic

#### [NEW] [watchdog.py](file:///Users/hiendang/project/src/sre/watchdog.py)
*   **Main Loop**: Polls every 60s.
*   **Health Checks**:
    *   `check_worker_health()`: Checks if `omni-worker` pods are in `CrashLoopBackOff` or `Error`.
    *   `check_db_health()`: Queries `pgpool-gateway` for node status and `rag_documents` accessibility.
    *   `check_cnpg_status()`: Queries the `Cluster` custom resource (if possible) or service endpoints.
*   **Auto-Fix Engine**:
    *   `remediate_auth_issue()`: Re-syncs DSN secrets if auth fails.
    *   `remediate_permission_issue()`: Runs `ALTER OWNER` if table ownership is wrong.
    *   `remediate_stale_image()`: Triggers a rollout-restart if the image version is mismatched.

### Phase 2: Deployment & RBAC

#### [NEW] [omni-watchdog.yaml](file:///Users/hiendang/project/deployments/omni-watchdog.yaml)
*   New Deployment for `omni-watchdog`.
*   Uses the same image as the worker (once rebuilt) but with a different entrypoint.

#### [NEW] [watchdog-rbac.yaml](file:///Users/hiendang/project/k8s/rbac/watchdog-rbac.yaml)
*   **ServiceAccount**: `omni-watchdog`.
*   **Role**: `omni-watchdog-role` with `get`, `list`, `watch` on Pods/Logs and `patch`, `update` on Deployments/Jobs.
*   **RoleBinding**: Ties them together in the `multi-agent` namespace.

### Phase 3: Notification Integration

#### [MODIFY] [watchdog.py](file:///Users/hiendang/project/src/sre/watchdog.py)
*   Integrate with the existing Telegram client to send "Fix Applied" alerts to the admin chat ID.

## Open Questions
*   **Build Trigger**: Should the watchdog have the power to delete and recreate the `cnpg-image-builder` job automatically?
*   **Scope**: Should it also monitor the `redis-cluster` and its bootstrapper?

## Verification Plan

### Automated Tests
*   Inject a wrong password into the worker ConfigMap and verify the watchdog detects the failure and resets it (or alerts).
*   Manually change a table owner in Postgres and verify the watchdog fixes it.

### Manual Verification
*   Simulate a `CrashLoopBackOff` by providing an invalid PYTHONPATH and monitor the watchdog's response.
