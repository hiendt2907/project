# Executor RBAC & network checklist

Omni executor consumes **`omni-actions`** and runs **`EXECUTE_MUTATE`** only on the executor deployment. Align manifests with `CLAUDE.md` (**never cluster-admin**, no cluster-wide Secret reads unless explicitly justified).

## Manifests

- Reference RBAC: [`k8s/rbac-executor-least-privilege.yaml`](../../k8s/rbac-executor-least-privilege.yaml).
- Deployments: [`k8s/deployments/omni-executor.yaml`](../../k8s/deployments/omni-executor.yaml) (verify `serviceAccountName`, bound `Role`/`RoleBinding`).

## Checklist

1. **Scope:** `Role` + `RoleBinding` in customer/analyst namespace(s); avoid `ClusterRole` unless read-only and narrowly scoped.
2. **Secrets:** No Secret `get/list/watch` unless a documented break-glass tool requires it; prefer ConfigMap references and CSI/driver patterns.
3. **Verbs:** Mutations limited to workloads needed for remediation (e.g. deployments, pods, configmaps) — not `*` on `*`.
4. **ServiceAccount:** Dedicated SA for executor; not shared with analyst/gateway.
5. **NetworkPolicy:** Egress to Kubernetes API, Kafka, Redis as required; align with single-egress customer docs where applicable (`docs/CUSTOMER_BANK_SINGLE_EGRESS.md`).
6. **Break-glass:** `kubectl_cluster` and HIGH-risk tools require env flags in prod — see `docs/runbooks/auto-execute-policy-matrix.md`.
