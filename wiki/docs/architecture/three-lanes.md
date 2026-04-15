# Three-lane evidence model (Proof-of-Fault)

Runtime lane selection: `pkg.reasoning.incident_matrix_profile.resolve_proof_lane` — precedence:

1. Annotation / label `omni_proof_lane` (if present on propagated labels)
2. **Incident training matrix** row (`config/incident_training_matrix.yaml`) — `workload_profile`, `expected_stage`
3. **Heuristics** (e.g. K8s failure tokens in hints)
4. Default: **`resource`**

## Lane summary

| Lane | ID | Mechanism | Matrix `expected_stage` (typical) |
|------|-----|-----------|-----------------------------------|
| **Resource** | `resource` | Baseline snapshot `dr` and/or PromQL z-scores (`z_cpu`, `z_mem`) vs threshold | `sigma_verify` |
| **State** | `state` | Deterministic pod/container failure signals (events, exit codes, OOM, etc.) — **no z-score requirement** | `state_confirm` |
| **App log** | `app_log` | Loki sustained 5xx / error lines when sigma is flat (API/Web workloads) | `log_surge_verify` |

## Two different “sigma” mechanisms (do not confuse)

1. **Baseline manifest (`baseline_snapshot.build_health_manifest_dict`)**  
   - Pulls instant PromQL scalars (defaults like `omni:node_cpu:z`, `omni:mem:z`).  
   - Sets `dr` true if `|z_cpu|` or `|z_mem|` exceeds `baseline_dr_z_threshold` (default **3.0**), or legacy CPU drift if enabled.  
   - Stored in Redis snapshot; consumed by `_proof_of_fault_gate` in `evidence_consumer.py`.

2. **`anomaly.three_sigma.ThreeSigmaGate`**  
   - Rolling window in **Redis** (`LPUSH`/`LTRIM`), per-metric z-score on the **newest** sample vs window mean/std.  
   - Anomaly when `|z| > 3`, window ≥ 3 points, `std >= 1e-9`.  
   - Used for metric-stream style gates, **not** the same code path as baseline PromQL z.

## OOM / crashes

Prefer **state** lane when evidence shows termination reason (e.g. `OOMKilled`): physical proof for mutate is **state**, not z-score.

See also: `docs/reports/incident-evidence-three-lanes.md`.
