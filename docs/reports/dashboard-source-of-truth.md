# Dashboard Source of Truth (Omni)

## Provisioning Contract
- Provision only 3 dashboards via `k8s/monitor/grafana-dashboards.yaml`.
- Canonical JSON sources:
  - `k8s/monitor/dashboards/omni_ops.json`
  - `k8s/monitor/dashboards/omni_security.json`
  - `k8s/monitor/dashboards/omni_learning.json`
- Sync helper: `scripts/sync_grafana_dashboard_configmaps.py`.

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

## Runtime Verification Snapshot (This Iteration)
- Pass: `make secret-gate`, env/mutate/classifier/docs gates, `make e2e-incident-matrix`, deploy rollouts.
- Blockers:
  - `infra_blocker`: `sigma_gate_ok=false` (insufficient sigma evidence in strict window).
  - `logic_blocker`: `trace_stage_matrix_ok=false` under strict proactive audit timing.
