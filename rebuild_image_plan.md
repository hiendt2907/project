# Implementation Plan: Rebuild Image & Cleanup Deployment

This plan outlines the steps to rebuild the Docker images with the latest fixes and then update the production deployment to use the new image, removing the temporary "hotfix" hacks (ConfigMaps and runtime pip installs).

## User Review Required

> [!IMPORTANT]
> This will trigger a full rebuild of the `omni-worker:latest` and Postgres custom images. The worker will be restarted once the build is complete.

## Proposed Changes

### Phase 1: Image Reconstruction

#### [RERUN] [cnpg-image-builder](kubectl-job)
*   Delete the existing completed job: `kubectl delete job cnpg-image-builder -n multi-agent`.
*   Re-apply the job to trigger the build: `kubectl apply -f k8s/jobs/cnpg-image-builder.yaml` (or equivalent manifest extracted from the cluster).

### Phase 2: Deployment Sanitization

#### [MODIFY] [omni-worker.yaml](file:///Users/hiendang/project/deployments/omni-worker.yaml)
*   **Remove** the custom `command`: `["/bin/bash", "-c", "pip install pgvector && python3 -m workers.omni_worker"]`.
*   **Remove** all `volumeMounts` related to `code-fix`.
*   **Remove** the `volumes` section for `omni-worker-fix`.
*   The deployment will now rely on the code and dependencies baked into the new image.

### Phase 3: Cleanup

#### [DELETE] [omni-worker-fix](kubectl-configmap)
*   Delete the temporary ConfigMap: `kubectl delete configmap omni-worker-fix -n multi-agent`.

## Verification Plan

### Automated Tests
*   Monitor the `cnpg-image-builder` pod logs to ensure the Docker build completes successfully.
*   Monitor `omni-worker` logs after rollout to ensure it starts without the "hotfix" and connects to the DB correctly.

### Manual Verification
*   Verify that the `pgvector` library is present in the container without the runtime `pip install` command.
*   Verify that the source files in `/app/src` match the latest workspace changes.
