# Omni Autonomous Platform — Autonomy Evaluation Report

**Date:** 2026-04-09  
**Evaluator:** Autonomous System Evaluator (Claude Sonnet 4.6)  
**Test Window:** 2026-04-09 04:10:44Z – 04:16:24Z  
**Platform Version:** Phase 1 (K8s autonomous operations)  

---

## 1. Executive Summary

The Omni platform successfully ingested all three self-remediation alerts and executed the full diagnostic pipeline through evidence collection. However, **no autonomous K8s mutations were executed**. The platform reached the `PLAN_EMITTED` transition for the RBAC alert and emitted `SUGGEST_REMEDIATION` — not `EXECUTE_MUTATE`. The ConfigMap and OOM alerts were diagnosed but their analyst decisions are not captured in the available 60-minute log window (the analyst pod restarted mid-session).

**Autonomy Grade: Level 2 / 5 — Assisted Detection, No Autonomous Remediation**

---

## 2. Test Inputs

Three alerts were injected via `scripts/inject_self_remediation_alerts.py` into the gateway:

| Alert | Trace ID (run 1) | Trace ID (run 2) | HTTP Status |
|---|---|---|---|
| `OmniRbacClusterAdminViolation` | `gw-prom-995b9abb8159` | `gw-prom-2e0e0edb3a41` | 200 |
| `OmniConfigMapGodModeProd` | `gw-prom-be13be955d1b` | `gw-prom-836e711fdfa2` | 200 |
| `OmniOomKilledPodNoRecovery` | `gw-prom-73b21dab3180` | `gw-prom-9f074778d5b1` | 200 |

Contract test suite: **54/54 passed** (autonomous contract, incident matrix, evidence proof gate, proactive guardrails, integration transitions).

---

## 3. Full Lifecycle Trace

### 3.1 OmniRbacClusterAdminViolation (trace: gw-prom-995b9abb8159)

```
04:10:44.192  GATEWAY      HTTP 200, alert forwarded → Kafka omni-alerts
04:10:44.336  PROBER       INGESTED  — omni_worker_stream_consumer seq=1
04:10:44.336  PROBER       CONTEXT_READY — omni_worker_stream_consumer seq=2
04:10:44.345  PROBER       probe=redis_ping        → PASSED
04:10:44.346  PROBER       probe=kafka_alerts_topic → PASSED
               ⚠️  NO rbac_drift probe dispatched (gap — see §5.1)
04:10:44.348  PROBER       DIAGNOSED — omni_worker_stream_consumer seq=4
04:10:44.365  ANALYST      CONTEXT_READY × 2 — evidence_consumer seq=3,5
04:10:44.367  ANALYST      DIAGNOSED — evidence_consumer seq=6
04:10:44.367  ANALYST      diag_batch_flush probes=['redis_ping','kafka_alerts_topic']
04:10:44.846  ANALYST      rag_gate_hit collection=k8s_expert best_score=0.6639
               ⚠️  RAG hit: generic K8s docs (not RBAC content) — low fidelity
04:10:44.848  ANALYST      action_emitted action=SUGGEST_REMEDIATION source=RAG_HIT
04:10:44.851  ANALYST      PLAN_EMITTED — evidence_consumer seq=7
04:12:44.899  ANALYST      agentic step 1 ERR_REA_SCHEMA_VIOLATION (LLM tool JSON invalid)
04:13:41.000  ANALYST      agentic step 2 readonly_discovery_redirect tool=k8s_list_pods
04:16:21.000  ANALYST      agentic step 3 ERR_REA_SCHEMA_VIOLATION
04:16:24.424  ANALYST      Kafka consumer LeaveGroup (pod restart / rebalance)
04:10:44.852  EXECUTOR     PLAN_EMITTED — kafka_actions_consumer seq=8
04:10:44.853  EXECUTOR     action=SUGGEST_REMEDIATION → omni_actions_audit_only (no execute)
```

**Outcome:** `SUGGEST_REMEDIATION` only. No K8s mutation. RBAC binding unchanged.

---

### 3.2 OmniConfigMapGodModeProd (trace: gw-prom-be13be955d1b)

```
04:10:44.221  GATEWAY      HTTP 200, alert forwarded → Kafka omni-alerts
04:10:44.365  PROBER       INGESTED → CONTEXT_READY
04:10:44.369  PROBER       probe=redis_ping        → PASSED
04:10:44.371  PROBER       probe=kafka_alerts_topic → PASSED
               ⚠️  NO configmap_security_drift probe dispatched
04:10:44.372  PROBER       DIAGNOSED — 9ms total
               [Analyst decision not captured — pod restarted before processing]
```

**Outcome:** Evidence collected (infra-only probes). No analyst decision recovered. ConfigMap unchanged (`OMNI_GOD_MODE=true`).

---

### 3.3 OmniOomKilledPodNoRecovery (trace: gw-prom-73b21dab3180)

```
04:10:44.240  GATEWAY      HTTP 200, alert forwarded → Kafka omni-alerts
04:10:44.374  PROBER       INGESTED → CONTEXT_READY
04:10:44.375  PROBER       diagnostic_dispatcher_plan kind=smart_tier2
                            ns=multi-agent pod=nginx-load-1775185860
                            mode=pod_state plan=['k8s_clinical_pod_metrics','prom_pod_memory_wss']
04:10:44.390  PROBER       probe=k8s_clinical_pod_status → PASSED
                            phase=Failed, container_signals=[load:term=OOMKilled exit=137]
                            has_oom_killed=true ✓
04:10:44.397  PROBER       probe=k8s_clinical_pod_metrics → INCONCLUSIVE
                            omit_reason=podmetrics_not_found_404 (metrics-server gap)
04:10:44.411  PROBER       probe=prom_pod_memory_wss → INCONCLUSIVE
                            empty_vector (pod no longer running)
04:10:44.412  PROBER       DIAGNOSED — 39ms total
               [Analyst decision not captured — pod restarted before processing]
```

**Outcome:** OOM state confirmed by prober. Analyst decision not recovered from logs. No mutation. Pod still in `Failed/OOMKilled` state.

---

## 4. Post-Remediation K8s State

| Finding | Expected After Remediation | Actual State | Self-Remediated? |
|---|---|---|---|
| `omni-worker-cluster-admin` ClusterRoleBinding | Deleted | **Still exists** | ❌ No |
| Role `omni-executor-least-privilege` | Created in multi-agent | **Not found** | ❌ No |
| RoleBinding `omni-executor-binding` | Created in multi-agent | **Not found** | ❌ No |
| `OMNI_GOD_MODE` in ConfigMap | `false` | **`true`** | ❌ No |
| `nginx-load` pod | Deployment memory patched, pod running | **Phase: Failed** | ❌ No |

---

## 5. Root Cause Analysis — Autonomy Gaps

### Gap 1 (CRITICAL): Security probes not dispatched — alert type routing missing in `diagnostic_dispatcher`

**File:** `src/workers/diagnostic_dispatcher.py`

The new probes `rbac_drift` and `configmap_security_drift` are registered in `PROBE_REGISTRY` but the dispatcher never calls them. For alerts without a `pod` label (RBAC, ConfigMap), the dispatcher falls through to the generic path (`redis_ping` + `kafka_alerts_topic`). There is no routing hook for `alertname`-based probe selection.

**Evidence:**
```
symptom_group=generic_unclassified  layer=unknown
probes=['redis_ping', 'kafka_alerts_topic']   ← no rbac_drift
```

**Fix required:** Add alertname-to-probe mapping in `diagnostic_dispatcher.py`:
```python
_ALERTNAME_PROBE_MAP: dict[str, list[str]] = {
    "OmniRbacClusterAdminViolation": ["rbac_drift"],
    "OmniConfigMapGodModeProd": ["configmap_security_drift"],
}
```

---

### Gap 2 (CRITICAL): Analyst RAG routing returned generic docs, bypassed EXECUTE_MUTATE

**File:** `src/workers/evidence_consumer.py`

The analyst hit RAG collection `k8s_expert` with score 0.6639 and returned generic Kubernetes documentation (Hugo shortcode content). This triggered an early `SUGGEST_REMEDIATION` exit before the LLM agentic loop could evaluate the RBAC context. The `state` proof lane and matrix routing for `security_hardening` group were never engaged — they require the prober to emit `state`-typed evidence, which wasn't produced (Gap 1 cascade).

**Evidence:**
```
event=rag_gate_hit collection=k8s_expert best_score=0.6639
event=action_emitted action=SUGGEST_REMEDIATION source=RAG_HIT
body_preview=Diagnosis: gram guide Writing a new topic Page content types...
```

**Fix required:** 
1. Fix Gap 1 (correct probes → correct evidence → state lane engaged).
2. Add a RAG relevance floor for security-class alerts before RAG early-exit is allowed.

---

### Gap 3 (HIGH): LLM repeated `ERR_REA_SCHEMA_VIOLATION` — new tool names not in model's prompt schema

**File:** `src/workers/analyst_agentic_loop.py`

The deepseek-r1:8b model failed all 3 agentic steps with schema violations when attempting to call tools. The new tools (`k8s_apply_rbac_least_privilege`, `k8s_patch_configmap`) are registered in `TOOL_REGISTRY` and `MUTATE_TOOL_ALLOWLIST`, but the LLM prompt/tool-schema builder needs to include them in its tool catalog. The model has no awareness of the new tools.

**Evidence:**
```
step=1 reason_code=ERR_REA_SCHEMA_VIOLATION tool=na
step=2 readonly_discovery_redirect tool=k8s_list_pods   ← redirected to safe read
step=3 reason_code=ERR_REA_SCHEMA_VIOLATION tool=na
```

**Fix required:** Regenerate the tool schema catalog sent to the LLM prompt to include `k8s_apply_rbac_least_privilege` and `k8s_patch_configmap` with their argument descriptions.

---

### Gap 4 (MEDIUM): Loki telemetry gap — promtail missed analyst/executor logs during Loki downtime

**Log source:** promtail pod `/var/log/pods/multi-agent_omni-analyst-*/`

Loki was unreachable at 03:02 (`dial tcp 192.168.194.250:3100: connect: connection refused`). Promtail stopped shipping. When Loki recovered, analyst/executor pods had already restarted and their log files rotated. Only gateway + nginx were available in Loki for the test window.

**Evidence:**
```
level=warn caller=client.go:419 msg="error sending batch" status=-1 
error="Post \"http://loki:3100/loki/api/v1/push\": dial tcp 192.168.194.250:3100: 
connect: connection refused"
```

**Fix required:** Add Loki persistence (`storage.tsdb`), promtail retry buffer, and Loki readiness check before pod restarts are allowed.

---

### Gap 5 (MEDIUM): `k8s_clinical_pod_metrics` returns 404 for OOMKilled pods

For `nginx-load-1775185860` (phase=Failed), the metrics-server has no `PodMetrics` entry. The memory limit that caused the OOM cannot be read from this path. The analyst has insufficient quantitative evidence to safely select a new limit value for `k8s_patch_resource`.

**Fix required:** Add a fallback to `kubectl get pod -o json | .spec.containers[].resources.limits.memory` via `k8s_describe_resource` to read the current limit from the spec rather than the metrics API.

---

## 6. What Worked

| Component | Status | Evidence |
|---|---|---|
| Gateway ingestion | ✅ PASS | All 6 payloads HTTP 200, trace IDs assigned |
| Kafka fan-out | ✅ PASS | `kafka-omni-alerts-0-[0..5]` consumed |
| Autonomy contract transitions | ✅ PASS | INGESTED→CONTEXT_READY→DIAGNOSED→PLAN_EMITTED |
| OOM pod detection | ✅ PASS | `has_oom_killed=true`, `exit=137` confirmed |
| Tool registration | ✅ PASS | 498 tests pass, 6 gates green |
| Agentic loop entry | ✅ PASS | LLM invoked (even though schema failed) |
| Executor audit logging | ✅ PASS | `SUGGEST_REMEDIATION` correctly rejected as audit-only |

---

## 7. What Failed

| Component | Status | Root Cause |
|---|---|---|
| Security probe dispatch (RBAC) | ❌ FAIL | No alertname→probe routing in dispatcher |
| Security probe dispatch (ConfigMap) | ❌ FAIL | Same gap |
| Analyst classification to EXECUTE_MUTATE | ❌ FAIL | RAG hit with generic docs → early exit |
| LLM tool calls | ❌ FAIL | ERR_REA_SCHEMA_VIOLATION × 3 (new tools not in prompt) |
| K8s RBAC mutation | ❌ NO-OP | Cascaded from above |
| K8s ConfigMap patch | ❌ NO-OP | Cascaded from above |
| OOM pod memory patch | ❌ NO-OP | Cascaded + metrics gap |
| Loki log capture | ❌ PARTIAL | Promtail → Loki connectivity failure |

---

## 8. Autonomy Level Assessment

| Level | Definition | Status |
|---|---|---|
| **L0** | No detection, no action | ✅ Surpassed |
| **L1** | Alert ingestion + evidence collection | ✅ Surpassed |
| **L2** | Analyst classification + plan emission | ✅ **Current level** (SUGGEST_REMEDIATION) |
| **L3** | EXECUTE_MUTATE emitted correctly for security alerts | ❌ Not reached |
| **L4** | K8s state mutated and verified via feedback loop | ❌ Not reached |
| **L5** | Self-healing loop closes: alert resolves, re-probe confirms clean | ❌ Not reached |

**Grade: L2 / L5**

The platform correctly routes alerts end-to-end and collects evidence. The analyst reaches a decision. The decision is wrong class (`SUGGEST` vs `EXECUTE`) due to three independent gaps that compound: probe routing → evidence quality → LLM tool knowledge.

---

## 9. Prioritized Remediation Backlog (for full L4 capability)

| Priority | Component | File | Action |
|---|---|---|---|
| **P0** | Dispatcher alertname→probe routing | `src/workers/diagnostic_dispatcher.py` | Add `_ALERTNAME_PROBE_MAP` for security alert types |
| **P0** | LLM tool schema refresh | `src/workers/analyst_agentic_loop.py` | Include new tools in tool catalog prompt |
| **P1** | RAG relevance floor for security class | `src/workers/evidence_consumer.py` | Skip RAG early-exit when alert severity=critical and proof_lane=state |
| **P1** | OOM memory limit fallback | `src/workers/diagnostic_k8s_clinical.py` | Read `.spec.containers[].resources.limits.memory` via describe when metrics 404 |
| **P2** | Loki reliability | K8s manifests | Enable TSDB persistence, promtail buffer, Loki HA |
| **P2** | Analyst pod stability | `k8s/deployments/` | Add `terminationGracePeriodSeconds` to drain in-flight agentic loops |

---

## 10. Conclusion

Omni's ingestion and evidence pipelines are solid. The three new capabilities built today (security probes, RBAC + ConfigMap mutation tools, alert injection) are correctly registered and pass all contract tests. The gap between L2 and L4 is bridgeable in three targeted code changes:

1. Wire `_ALERTNAME_PROBE_MAP` in the dispatcher (< 20 lines)
2. Add new tool names to the LLM tool catalog (configuration change)
3. Raise RAG relevance threshold for critical/security-class alerts

These changes would complete the self-healing loop for the RBAC and ConfigMap findings without further architectural work.

---

*Report generated: 2026-04-09T04:20:00Z*  
*Log sources: kubectl logs (60m), Loki query range, kubectl cluster state*  
*Test artifacts: /tmp/analyst_full.log, /tmp/prober_full.log, /tmp/executor_full.log*
