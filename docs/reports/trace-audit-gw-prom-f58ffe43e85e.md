# Trace audit: `gw-prom-f58ffe43e85e`

**Scenario:** Lab nginx missing ConfigMap (`scripts/lab_nginx_test_missing_configmap_e2e.sh`).  
**Lab date:** 2026-04-09. **Audit closed:** 2026-04-11.

## Loki (corrected epoch / multi-day window)

- **33 rows, 3 streams:** prober 11, analyst 19, executor 3.
- LogQL: `{namespace="multi-agent", pod_name=~"omni-.*"} |= "gw-prom-f58ffe43e85e"`

## Pipeline (high level)

| Stage | Component | Notes |
|-------|-----------|--------|
| INGESTED | prober | `NginxTestContainerWaitingFaultLab`, pod `nginx-test-84965bf78b-kkh6z` |
| CONTEXT_READY | prober / analyst | Kafka `omni-alerts` consumed |
| DIAGNOSED | prober | `k8s_clinical_pod_events` → raw contains missing ConfigMap name |
| PLAN_EMITTED | analyst | `rag_hints_buffered`, `proof_lane=resource`, `broken_spec=True` |
| Agentic | analyst | Historical: `ERR_REA_SCHEMA_VIOLATION` on describe ConfigMap (schema); fallback `k8s_rollout_restart` → `ERR_REA_SIGMA_GATE_BLOCKED` before fixes |
| Action | analyst → executor | `SUGGEST_REMEDIATION` via `PROOF_OF_FAULT_GATE`; **no** `action_feedback` (no mutate) — expected for suggest-only path |

## Defects fixed (code)

1. **2026-04-10:** `DescribeResourceArgs.resource_type` extended so `k8s_describe_resource` accepts ConfigMap/Secret (see `src/workers/k8s_cluster_tools.py`).
2. **2026-04-10:** Broken-spec fallback in `_emit_agentic_mutate_if_any` must not blindly use `k8s_rollout_restart`; prefer `k8s_create_or_patch_configmap` when evidence shows missing CM.
3. **2026-04-11:** `fallback_lane_override="state"` so `blind_lane_eff` reaches `_proof_of_fault_gate` as **state** lane (fast-track without sigma when matrix pinned `resource`).

**Files:** `src/workers/evidence_consumer.py` — `fallback_lane_override`, `blind_lane_eff`.  
**Tests:** `tests/test_configmap_remediation.py` — `test_fallback_lane_is_state_for_broken_spec_cm`, `test_fallback_lane_is_none_for_crashloop`.

## Verify

```bash
pytest tests/test_configmap_remediation.py -q
# Optional full lab (mutates cluster):
# STRICT_ASSERT=0 bash scripts/lab_nginx_test_missing_configmap_e2e.sh
```

## Canonical pointers

- [`project-memory.md`](project-memory.md) — LabVsRealAlertTesting + FailurePatterns.
- [`../vendor/knownbase.md`](../vendor/knownbase.md) — symptom FailedMount + `ERR_REA_SIGMA_GATE_BLOCKED` entry.
- [`lab_nginx_missing_configmap_e2e.md`](lab_nginx_missing_configmap_e2e.md) — lab procedure.
