# Auto-execute (EXECUTE_MUTATE) policy matrix

Single reference for planner emission vs executor boundary vs advisory kill-switch. **Prod defaults are fail-closed** (`CLAUDE.md`).

## Environment defaults

| Flag | Prod posture | Lab note |
|------|----------------|----------|
| `OMNI_AUTO_EXECUTE_ENABLED` | `false` (explicit enable for mutate) | Dev role may still execute when `OMNI_ENV_MODE=dev` |
| `OMNI_UNRESTRICTED_TOOL_EXECUTION` | **`false`** — executor uses mutate allowlist | Set `true` only for controlled experiments |
| `OMNI_SHADOW_OS_MODE` | `true` → no SDK mutate (runbook path) | Contract: skipped feedback + tombstone from executor |
| `OMNI_KUBECTL_CLUSTER_MUTATE_ALLOWED` | **`false`** — `kubectl_cluster` denied in prod | Break-glass only |
| `OMNI_HIGH_RISK_MUTATE_ALLOWED` | **`false`** — blocks delete pod / patch secret / apply RBAC in prod | Pair with RBAC review |
| `OMNI_SIEM_SUGGEST_ONLY` | Typically `true` — SIEM stays suggest/HITL | No `EXECUTE_MUTATE` for SIEM-only lane |

## Risk tiers (executor — `pkg/executor/mutate_governance.py`)

| Tier | Tools |
|------|--------|
| LOW | `k8s_rollout_restart` |
| MEDIUM | `k8s_scale_deployment`, `k8s_patch_resource`, `k8s_patch_configmap`, `k8s_create_or_patch_configmap` |
| HIGH | `k8s_patch_secret`, `k8s_delete_pod`, `k8s_apply_rbac_least_privilege` |
| BREAK_GLASS | `kubectl_cluster` |

Prod blocks HIGH unless `OMNI_HIGH_RISK_MUTATE_ALLOWED=true`; blocks BREAK_GLASS unless `OMNI_KUBECTL_CLUSTER_MUTATE_ALLOWED=true`.

## Allowlists

- **Executor mutate allowlist:** `workers/autonomous_execute.py` — `MUTATE_TOOL_ALLOWLIST` / `K8S_SDK_MUTATING_TOOL_NAMES`.
- **Read-only blocked on EXECUTE_MUTATE:** `READONLY_TOOL_ALLOWLIST`.
- **Planner / evidence gates:** proof-of-fault, diagnostic invariants (`INV_*`), namespace isolation — see `evidence_consumer.py` (`_emit_agentic_mutate_if_any`).
- **Advisory dangerous tools:** `AdvisoryModeKillSwitch` — keep aligned with executor policy when changing tool names.

## Verification & “resolved” semantics

- Post-mutate verification: `run_verify_probes` + deployment rollout gate in `autonomous_feedback_loop.py`.
- Trace orchestrator: on terminal success after state-machine verify, phase is set to **`resolved`** via `mark_trace_orchestrator_resolved_verified`.
- **Pre-flight dry-run:** extend rollout evidence snapshot pattern to other mutators over time; optional server-side dry-run where the SDK supports it.

## Observability

- `omni_executor_execute_skipped_total{reason}` — `shadow_os`, `auto_execute_disabled`, `rate_limited`.
- Prometheus alert: `OmniExecutorMutateSkippedBurst` in `k8s/monitor/prometheus-rules-omni-health.yaml`.
