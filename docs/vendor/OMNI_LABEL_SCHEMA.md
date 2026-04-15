# Omni Label Schema (Golden Link)

Single vocabulary from Prometheus alert → Kafka → Redis → RAG experience. See also [`config/omni_label_schema.yaml`](../../config/omni_label_schema.yaml).

## Resource DNA (Kubernetes objects)

| Label | Role | Example |
|-------|------|---------|
| `app.kubernetes.io/name` | Component | `omni-executor` |
| `app.kubernetes.io/part-of` | System | `omni-platform` |
| `omni.io/managed` | Omni may operate | `true` |
| `omni.io/symptom-group` | Symptom lane | `security_hardening` |
| `omni.io/criticality` | Priority | `P0` |
| `omni.io/team` | Escalation | `platform-ops` |

## Signal DNA (Prometheus / inject)

| Label | Role |
|-------|------|
| `alertname` | Dispatcher primary key |
| `severity` | Severity |
| `namespace` | Scope |
| `deployment` | Workload |
| `drift_type` | Drift class |
| `omni_verify_required` | If `false`, skip post-mutate SDK verify for this alert |

## Telemetry DNA (logs / Kafka envelopes)

| Field | Role |
|-------|------|
| `trace_id` | End-to-end correlation |
| `event_type` | e.g. `diagnostic_evidence_publish`, `mutate_executed` |
| `symptom_group` | Aligns with `omni.io/symptom-group` |

## Incident lifecycle (`omni.io/*` in Redis / envelopes)

| Key | Role | Example |
|-----|------|---------|
| `omni.io/incident-id` | Unique case id (usually equals `trace_id`) | `gw-prom-…` |
| `omni.io/incident-state` | Autonomy state | `INGESTED`, `VERIFIED_SUCCESS`, … |
| `omni.io/symptom-group` | Prober lane | `security_hardening` |
| `omni.io/layer` | Layer | `infra`, `security`, `workload` |

Structured `extracted_fact` in evidence items complements these keys.

## Resolution DNA (RAG / `action_experience` payload, post-verify)

| Key | Role | Example |
|-----|------|---------|
| `omni.io/root-cause-id` | Taxonomy id | `rbac_drift_detected` |
| `omni.io/root-cause-desc` | Short RCA text | Human-readable |
| `omni.io/resolution-tool` | Tool that fixed | `k8s_apply_rbac_least_privilege` |
| `omni.io/verify-method` | How verified | `sdk_probe_verify` |

## Automated labeling flow

1. **Diagnostic:** Prober emits `extracted_fact`; Analyst uses `drift_type`, `alertname`, symptom group / layer.
2. **Verification:** After Executor, SDK verify when `omni_verify_required` is not `false` and settings allow.
3. **Upsert:** On PASSED, Analyst writes Resolution DNA into `COLLECTION_ACTION_EXPERIENCE` (`action_experience`).
