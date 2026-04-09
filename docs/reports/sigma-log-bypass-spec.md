# Sigma log bypass (Loki sustained 5xx)

**Scope:** Optional path in `evidence_consumer._proof_of_fault_gate` when **`proof_lane` is `app_log`** (three-lane model): if baseline sigma (`dr` / z-score) is false, the gate may still pass when Loki shows sustained HTTP 5xx (or JSON app errors) for the target pod.

## Preconditions (all required)

- `OMNI_SIGMA_LOG_BYPASS_ENABLED=true` on the worker (ConfigMap / env).
- `OMNI_LOKI_BASE_URL` points at a reachable Loki `query_range` API (service DNS in-cluster).
- Namespace is allowed (`autonomous_allowed_namespaces` / `namespace_allowed`).
- Workload is classified **API/Web**: matrix row `workload_profile: api_web` for the alert **or** RAG text heuristic (`is_api_web_workload`).
- Evidence batch contains **namespace** and **pod** identity for LogQL (`namespace_pod_from_batch`).

## Behavior

- Probe: `workers/log_surge_probe.evaluate_log_surge_sigma_bypass` — access-log style lines and JSON lines; ratio of 5xx vs parsed lines vs `OMNI_LOG_SURGE_MIN_RATIO` (default **0.01** = 1%) over `OMNI_LOG_SURGE_WINDOW_SEC` (default 300s).
- Success: gate returns `proof_ok` with `sigma_bypass_via_log_surge` in meta; log `event=log_surge_sigma_bypass_ok`.
- Loki HTTP/parse failure: `ERR_REA_LOG_SOURCE_UNAVAILABLE` — escalate human, **no** mutate (fail-closed).

## Environment variables

| Variable | Role |
|----------|------|
| `OMNI_SIGMA_LOG_BYPASS_ENABLED` | Master switch (default false). |
| `OMNI_LOKI_BASE_URL` | Loki base URL. |
| `OMNI_LOG_SURGE_WINDOW_SEC` | Lookback window (s). |
| `OMNI_LOG_SURGE_MIN_LINES` | Minimum parsed lines before ratio applies. |
| `OMNI_LOG_SURGE_MIN_RATIO` | Minimum 5xx fraction (e.g. 0.01). |
| `OMNI_LOG_SURGE_LINE_LIMIT` | Loki line cap. |
| `OMNI_LOG_SURGE_HTTP_TIMEOUT_SEC` | HTTP timeout for Loki. |

## Relationship to three lanes

When `OMNI_PROOF_LANE_ENABLED=true`, log bypass runs **only** for **`app_log`** proof lane. **Resource** lane does not use Loki for sigma substitution. **State** lane fast-tracks on deterministic K8s evidence without sigma.
