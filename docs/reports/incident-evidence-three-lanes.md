# Three evidence lanes (Proof-of-Fault)

Runtime resolution: `pkg.reasoning.incident_matrix_profile.resolve_proof_lane` — **annotation** (`omni_proof_lane` on labels/annotations) **>** **matrix row** (`pick_matrix_row_for_batch`, including `series_label_defaults` and `workload_profile: api_web`) **>** **state heuristic** (K8s failure tokens in hint/labels) **>** **default `resource`**.

| Lane | ID | Mechanism | `expected_stage` (matrix) |
|------|-----|-----------|----------------------------|
| Resource | `resource` | Redis baseline `dr` / z-score + observation window | `sigma_verify` |
| State | `state` | Deterministic pod/container failure signals; **no** z-score | `state_confirm` |
| App log | `app_log` | Loki sustained 5xx rate when sigma is flat | `log_surge_verify` |

**OOM / termination:** Prefer **state** lane when evidence is termination reason (e.g. `OOMKilled`); root cause may still be memory pressure, but the proof for mutate is **physical state**, not z-score.

**Override:** Set `omni_proof_lane` on Prometheus labels (propagated into `canonical_query_snippet` JSON) to force a lane for lab or policy.

See also: [sigma-log-bypass-spec.md](sigma-log-bypass-spec.md), [incident_training_matrix.yaml](../../config/incident_training_matrix.yaml).
