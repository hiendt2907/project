# ADR — RBAC for omni-executor / mutate path (draft, lab-oriented)

**Bối cảnh tổng thể:** [OMNI_PROJECT_CANONICAL.md](OMNI_PROJECT_CANONICAL.md) §9.

## Context

In split topology, **`omni-executor`** consumes `omni-actions` and runs Kubernetes SDK tools via [src/workers/autonomous_execute.py](../../src/workers/autonomous_execute.py). Lab manifests often bind a **broad** ServiceAccount (`omni-worker`) for mutate operations. Production should **narrow** verbs and resources to the minimum required by the allowlisted tools.

## Decision (draft)

1. **Inventory** mutate-capable tool names from `K8S_SDK_MUTATING_TOOL_NAMES` / execution registry in `autonomous_execute.py` and policy in `execution/`.
2. **Map** each tool to Kubernetes API groups/resources:
  - `k8s_rollout_restart`, patch-style tools → `apps/deployments`, `apps/statefulsets`, `apps/daemonsets` (verbs: `get`, `patch`, `update` as needed; `deployments/rollback` subresource where applicable).
  - Namespace-scoped resources → **Role** + **RoleBinding** in `multi-agent` (or target namespaces) instead of `cluster-admin`.
3. **Cluster-scoped** reads (nodes, cluster metadata) only if a tool requires them — prefer **ClusterRole** with read-only verbs for list/get.

## Verification checklist

Before rolling out narrowed RBAC:

```bash
kubectl auth can-i patch deployment -n multi-agent --as=system:serviceaccount:multi-agent:<SA>
kubectl auth can-i create deployments/rollback -n multi-agent --as=system:serviceaccount:multi-agent:<SA>
# Add per-tool checks from the inventory above.
```

Re-run `bash scripts/e2e_incident_matrix.sh` with at least one `EXECUTE_MUTATE` scenario after RBAC changes.

## Status

**Draft** — full verb matrix should be completed when the allowlist stabilizes for a target environment. See [master_plan_v3_review_report.md](master_plan_v3_review_report.md) §13/§15 for historical note on wide SA in lab.