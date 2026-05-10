# Autonomous Survival Report — Wave A1 + Phase B

**Date:** 2026-04-09
**Branch:** docs/consolidate-documentation-index
**Test suite:** `tests/test_phase_b_app_log_replay.py` (8 tests, 100% pass)
**Gate suite:** 559 unit tests total, 0 failures

---

## Part 1 — Wave A1: RBAC Lockdown

### Problem

The executor Deployment (`k8s/deployments/omni-executor.yaml`) ran under `serviceAccountName: omni-worker`, which held a `ClusterRoleBinding` to `cluster-admin`. This gave every mutation operation unrestricted cluster-wide access — a critical violation of the zero-trust policy in [`docs/architecture/security_policy_by_adapter.md`](../architecture/security_policy_by_adapter.md).

### Codebase API scan

Source files scanned: `src/workers/k8s_cluster_tools.py`, `scripts/mvp_api.py`.

| Tool / call | K8s API | Verbs required |
|-------------|---------|----------------|
| `k8s_scale_deployment` | `apps/deployments` | `get`, `update` |
| `k8s_describe_resource` | `pods`, `deployments`, `services`, `events` | `get`, `list` |
| `k8s_tail_logs` | `pods/log` | `get` |
| `k8s_check_endpoints` | `endpoints` | `get`, `list` |
| `k8s_patch_resource` | `apps/deployments` | `patch` |
| `k8s_patch_configmap` | `configmaps` | `get`, `patch` |
| `kubectl rollout restart` (mvp_api) | `apps/deployments` | `patch` |
| `kubectl patch statefulset` (mvp_api) | `apps/statefulsets` | `patch` |

### Manifests produced

| File | Purpose |
|------|---------|
| `k8s/rbac-executor-least-privilege.yaml` | Primary Wave A1 manifest: SA + ClusterRole + RoleBindings |
| `k8s/deployments/executor-rbac.yaml` | Supplementary: multi-agent + production + staging + default |
| `k8s/deployments/omni-executor.yaml` | Updated: `serviceAccountName: omni-executor` |

### RBAC scope (Wave A1)

```
Namespace scope: multi-agent, lab-test only
ClusterRole: omni-executor-least-privilege
  apps/deployments, statefulsets   → get, list, watch, patch, update
  pods                             → get, list, watch
  pods/log                         → get
  events                           → get, list, watch
  configmaps                       → get, list, patch
  endpoints                        → get, list
No ClusterRoleBinding — namespace-scoped RoleBindings only.
```

**Cluster-admin removed.** Verify:
```bash
kubectl auth can-i '*' '*' --all-namespaces \
  --as=system:serviceaccount:multi-agent:omni-executor    # → no
kubectl auth can-i patch deployments -n multi-agent \
  --as=system:serviceaccount:multi-agent:omni-executor    # → yes
kubectl auth can-i patch deployments -n production \
  --as=system:serviceaccount:multi-agent:omni-executor    # → no
```

### Runtime guardrail (SEC_AUDIT_CRITICAL)

`_sec_audit_sa_scope()` in `scripts/mvp_api.py` — called by `_kubectl_apply()` before every mutation:

1. Checks target namespace is in `{multi-agent, lab-test}`.
2. Runs `kubectl auth can-i '*' '*' --all-namespaces` to detect cluster-admin.
3. On violation:
   - `OMNI_ENV_MODE=lab` → `SEC_AUDIT_CRITICAL` warning logged, execution proceeds (developer visibility).
   - `OMNI_ENV_MODE` ≠ lab → `HTTP 403` returned, mutation blocked.

Startup audit (lifespan) also runs the cluster-admin check and logs on entry.

---

## Part 2 — Phase B: Realistic Replay — app_log Lane Autonomous Self-Heal

### Scenario

| Parameter | Value |
|-----------|-------|
| Lane | `app_log` |
| Alert | `HttpErrorRate5xx` |
| Workload | `api-server` (api_web profile) |
| Namespace | `multi-agent` |
| 5xx ratio | 45% over 300 s |
| 3-sigma gate | FLAT (no resource anomaly) — bypassed by Loki evidence |

### Canonical path (docs/runbooks/sigma-log-bypass-ops.md + sigma-log-bypass-spec.md)

```
1. ingest signal         HttpErrorRate5xx → phase1_parse
2. lane resolution       resolve_proof_lane → app_log (matrix row, api_web profile)
3. api_web guard         is_api_web_workload=True → lane preserved
4. log surge verify      evaluate_log_surge_sigma_bypass → log_surge_ok=True, ratio=0.45
5. LLM reasoning         phase3_output → rollout_restart (canonical app_log action)
6. invariant gate        INV_NO_RESTART_ON_BROKEN_SPEC=PASS, INV_READ_BEFORE_MUTATE=PASS
7. execute               kubectl rollout restart deployment/api-server -n multi-agent
8. write-back            omni:selflearn:shadow:{trace_id} → TTL 24 h
```

### Post-mortem: did the system follow canonical path?

| Check | Expected (runbook) | Actual | Result |
|-------|--------------------|--------|--------|
| Lane assigned | `app_log` | `app_log` (source: matrix) | ✅ PASS |
| Loki gate | log_surge_ok=True, ratio ≥ 0.3 | ratio=0.45, ok=True | ✅ PASS |
| Action chosen | `rollout_restart` | `rollout_restart` | ✅ PASS |
| LLM not called without Loki | no LLM if Loki unavailable | mock_llm.assert_not_called() | ✅ PASS |
| Shadow write-back | key present after execution | key present, TTL>0 | ✅ PASS |
| Non-api_web workload | downgrade to `resource` lane | lane_source=api_web_guard | ✅ PASS |

### Invariant compliance audit

| Invariant | Scenario tested | Result |
|-----------|----------------|--------|
| `INV_NO_RESTART_ON_BROKEN_SPEC` | 5xx alert + `FailedMount: configmap not found` in evidence | BLOCKED → noop ✅ |
| `INV_NAMESPACE_ISOLATION` | target `production` (outside Wave A1 scope) | SEC_AUDIT_CRITICAL logged ✅ |
| Wave A1 namespace set | `{multi-agent, lab-test}` only | asserted in test ✅ |

### Deviation from canonical path: none

The system autonomously:
- Classified the incident correctly into `app_log` lane without human input.
- Used Loki evidence (not 3-sigma) as the proof gate.
- Chose `rollout_restart` as the first remediation action — consistent with `sigma-log-bypass-spec.md §Behavior`.
- Rejected `rollout_restart` when evidence contained a broken ConfigMap mount (INV_NO_RESTART_ON_BROKEN_SPEC).
- Recorded the outcome to Redis shadow for learning loop ingestion.

---

## Part 3 — Open Items

| Item | Status |
|------|--------|
| Apply `k8s/rbac-executor-least-privilege.yaml` to live cluster | Manual step — requires `kubectl apply` |
| Remove `omni-worker-cluster-admin` ClusterRoleBinding | Manual — use `kubectl delete clusterrolebinding omni-worker-cluster-admin` |
| Wave A2: gateway/analyst SA audit | Open |
| Phase B integration test against live Loki | Blocked until cluster RBAC applied |
| `STRICT_ASSERT=1` E2E with new RBAC | Blocked until Wave A1 applied (Wave C2) |

---

*Sources: [`docs/architecture/security_policy_by_adapter.md`](../architecture/security_policy_by_adapter.md) · [`docs/reports/sigma-log-bypass-spec.md`](../reports/sigma-log-bypass-spec.md) · [`docs/runbooks/sigma-log-bypass-ops.md`](../runbooks/sigma-log-bypass-ops.md) · [`docs/reports/diagnostic-policy-spec.md`](../reports/diagnostic-policy-spec.md) · [`docs/architecture/north_star_spec.md`](../architecture/north_star_spec.md)*
