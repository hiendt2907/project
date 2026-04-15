# Runbook: Evidence lanes, sigma, and proof-of-fault

For automation: use **symptom → check → action** order. Code reference: `src/workers/evidence_consumer.py` (`_proof_of_fault_gate`).

## Preconditions

- **Critical evidence:** `critical_evidence_present(batch)` must be true or gate returns `ERR_REA_NO_PHYSICAL_PROOF`.
- **Baseline snapshot:** Redis key from `baseline_snapshot` (see `REDIS_KEY_SNAPSHOT` in code) supplies `dr`, `z_cpu`, `z_mem`.

## Lane: `resource` (Prometheus / baseline z)

**Symptom:** Proof lane = `resource`; mutate blocked with `ERR_REA_SIGMA_GATE_BLOCKED`.

**Checks**

1. Read snapshot: `sigma_ok` requires `dr == true` **OR** `|z_cpu| >= threshold` **OR** `|z_mem| >= threshold` (threshold default **3.0**, `baseline_dr_z_threshold`).
2. If sigma not OK → gate fails before observation window fills.
3. If sigma OK → Redis counter `omni:proof_of_fault:window:{trace}` must reach `autonomous_sigma_observation_window` (default 1+).

**Corrective actions**

- **Z-score below threshold but real incident:** Confirm recording rules (`omni:node_cpu:z`, `omni:mem:z`) and Prometheus connectivity. Fix mis-scrape or wrong cluster.
- **Threshold too strict for lab:** Adjust `baseline_dr_z_threshold` via settings (document change; avoid prod loosening without policy).
- **Wrong lane:** If failure is pod/OOM/crash-dominant, ensure matrix/heuristic selects **`state`** lane (see below).

## Lane: `state` (K8s physical proof)

**Symptom:** OOMKilled, `CrashLoopBackOff`, eviction, etc.

**Behavior:** Sigma is **treated as OK** (`sigma_bypass_reason: state_lane_physical_proof`). Observation window still increments via Redis; effective need is 1 pass for state path (see code).

**Corrective actions**

- Ensure probes populate `extracted_fact` with termination reasons / events so `critical_evidence_present` and lane resolution succeed.
- If stuck at `ERR_REA_NO_PHYSICAL_PROOF`: widen diagnostic probes or fix RBAC so prober can read pod status/events.

## Lane: `app_log` (Loki log surge)

**Symptom:** API/Web workload, sigma flat, errors mention log bypass.

**Checks**

- `omni_sigma_log_bypass_enabled` must be **true**.
- `is_api_web_workload` (matrix `api_web` or RAG text heuristic) and **namespace allowlist** (`env_mode` / namespace policy).
- `omni_loki_base_url` set.
- `evaluate_log_surge_sigma_bypass`: sustained **500/503/504** in access logs or JSON app lines; min lines/ratio from `omni_log_surge_*` settings.

**Corrective actions**

- **Loki unavailable** (`ERR_REA_LOG_SOURCE_UNAVAILABLE`): fix Loki URL, network, or credentials; check prober logs for `loki_error`.
- **Insufficient 5xx ratio:** increase window, lower `min_ratio` carefully, or fix app (not a false positive—verify traffic).
- **Not api_web:** adjust matrix row for `prometheus_alert` or rely on RAG match text for HTTP/API keywords.

## Legacy mode (`omni_proof_lane_enabled` = false)

Sigma/log bypass follow older combined path: log surge can satisfy sigma when **sigma_ok** is false (same `evaluate_log_surge_sigma_bypass`).

## `ThreeSigmaGate` (Redis rolling metric) vs baseline z

If tooling reports **Redis** `3sigma:metric:*` keys: that is `anomaly.three_sigma.ThreeSigmaGate` — **anomaly when |z| > 3** on the rolling window.

Baseline **dr** in proof gate uses **PromQL** z-scalars from snapshot, not `ThreeSigmaGate` directly.

## Escalation

- Telegram / human escalation paths are triggered from evidence consumer on specific failures (e.g. log unavailable). Search `emit_telegram_escalation` in workers for details.
