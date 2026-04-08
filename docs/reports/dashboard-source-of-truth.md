# Dashboard Source of Truth (Omni)

## Provisioning Contract
- Provision only 5 dashboards via `k8s/monitor/grafana-dashboards.yaml`.
- Canonical JSON sources:
  - `k8s/monitor/dashboards/omni_ops.json`
  - `k8s/monitor/dashboards/omni_security.json`
  - `k8s/monitor/dashboards/omni_learning.json`
- `k8s/monitor/dashboards/omni_pod_resources.json`
- `k8s/monitor/dashboards/omni_node_resources.json`
- Sync helper: `scripts/sync_grafana_dashboard_configmaps.py`.
- Community imports used as reference baseline:
  - Grafana ID `16698` (Pods Improved)
  - Grafana ID `3320` (Kubernetes Node Exporter Full)

## Omni Ops (Panel -> Signal)
- Gateway 200 rate -> `sum(rate(omni_gateway_requests_total{status="200_ok"}[5m]))`
- Auto verify success ratio -> `omni_proactive_verify_total{outcome="success"}`
- Active firing alerts -> `ALERTS{alertstate="firing"}`
- 3-sigma observability -> `omni:node_cpu:z`, `omni:mem:z`
- Action/result trend -> `omni_proactive_outcome_total{outcome=...}`
- Trace lifecycle evidence -> Loki logs filtered by `trace_id`

## Omni Security (Panel -> Signal)
- Governance violations -> Loki count `ERR_GOV_*`
- Proof/Sigma gate blocks -> Loki count `ERR_REA_SIGMA_GATE_BLOCKED` + `ERR_REA_NO_PHYSICAL_PROOF`
- Unauthorized mutation attempts -> Loki count `ERR_GOV_UNAUTHORIZED_MUTATION`
- Reason-code forensic stream -> Loki regex `ERR_GOV_|ERR_REA_`
- Release gate checklist -> static runbook block (`secret/env-mode/mutate-only/classifier/docs`)

## Omni Learning (Panel -> Signal)
- Learning upsert trend -> `sum(rate(omni_learning_upserts_total[5m]))`
- Verified success/fail trend -> `omni_proactive_verify_total{outcome="success|failed"}`
- Planner fallback rate -> Loki count `PLANNER_FALLBACK`
- Proof gate block rate -> Loki counts for proof/sigma reason codes
- Trace-linked learning evidence -> Loki regex `learning_upsert|action_experience|VERIFIED_SUCCESS|PLAN_EMITTED`

## Omni Pod Resources (Panel -> Signal)
- Pod CPU usage (exclude `kube-system`) -> `container_cpu_usage_seconds_total{job="kubernetes-nodes-resource"}`
- Pod memory working set (exclude `kube-system`) -> `container_memory_working_set_bytes{job="kubernetes-nodes-resource"}`
- Pod CPU usage vs request -> usage + `kube_pod_container_resource_requests{resource="cpu"}`
- Pod memory usage vs request -> usage + `kube_pod_container_resource_requests{resource="memory"}`
- Kafka consumer lag proxy -> `sum by(app) (omni_worker_lag_size)`
- Stuck consumer heuristic -> `sum((max_over_time(omni_worker_lag_size[15m]) > bool 0) * (changes(omni_worker_lag_size[15m]) == bool 0))`
- Redis stream backlog / DLQ -> `sum by(stream) (max_over_time(omni_redis_stream_backlog[30m]))` + Loki log panel (`dlq|omni-dlq`)
- Kafka pipeline forensic logs -> Loki log panel (`kafka|omni-alerts|omni-actions|omni-action-feedback|consumer`)

## Omni Node Resources (Panel -> Signal)
- Node CPU usage -> `node_cpu_seconds_total{mode="idle"}`
- Node memory usage -> `node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes`
- Node filesystem used -> `node_filesystem_avail_bytes / node_filesystem_size_bytes`
- Node allocatable -> `kube_node_status_allocatable{resource=~"cpu|memory"}`

## Runtime Verification Snapshot (This Iteration)
- Pass: `make secret-gate`, env/mutate/classifier/docs/nonimpact/learning gates, deploy rollouts, `bash scripts/e2e_incident_matrix.sh` (matrix registry run, 31/31 pass with `STRICT_ASSERT=0`).
- Blockers:
  - `infra_blocker`: `sigma_gate_ok=false` (insufficient sigma evidence in strict window).
  - `logic_blocker`: `trace_stage_matrix_ok=false` under strict proactive audit timing.
