# Runbook: Enable sigma log bypass (lab)

1. Set on **omni-analyst** (or worker role consuming evidence) ConfigMap:
   - `OMNI_SIGMA_LOG_BYPASS_ENABLED=true`
   - `OMNI_LOKI_BASE_URL=http://loki.<monitor-namespace>.svc.cluster.local:3100` (adjust to your Loki service)
   - Optional tuning: `OMNI_LOG_SURGE_MIN_RATIO`, `OMNI_LOG_SURGE_WINDOW_SEC`
2. Rollout: `make deploy-worker` (or equivalent) after `make docker-worker`.
3. Verify: `kubectl logs deploy/omni-analyst -n <ns> | grep log_surge_sigma_bypass_ok` after an `app_log` scenario with injected 5xx logs.
4. **Never** hardcode cluster IPs or secrets in repo; use service DNS only.

When `OMNI_PROOF_LANE_ENABLED=true` (default), bypass applies only when **`proof_lane`** resolves to **`app_log`**.
