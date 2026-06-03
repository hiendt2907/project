<!-- Generated: 2026-05-22 | Files scanned: 270 Python + 65 TS/TSX | Token estimate: ~950 -->

# Architecture — Omni SRE

## System Type
Async-first multi-agent SRE automation. Single K8s cluster (OrbStack), namespace `multi-agent`.

## Inbound Signal Paths

```
HTTP alerts      POST /webhook/prometheus ──→ omni-gateway ──→ kafka: omni-alerts
SIEM incidents   FinGuard Redis XREADGROUP ──→ omni-siem-bridge ──→ kafka: omni-alerts
SIEM raw         Redis stream:siem_evidence_raw ──→ omni-evidence-adapter ──→ kafka: omni-diagnostic-evidence
Remote agents    POST /agent/push (Linux agents) ──→ omni-gateway ──→ kafka: omni-diagnostic-evidence
```

## Core Pipeline

```
kafka: omni-alerts
  └─→ omni-prober (kafka_alerts_loop)
        ├─ diagnostic_dispatcher → K8s SDK probes + PromQL
        └─→ kafka: omni-diagnostic-evidence

kafka: omni-diagnostic-evidence
  └─→ omni-analyst (kafka_evidence_loop)
        ├─ evidence_batch (quorum by trace_id)
        ├─ RAG gate (Redis HNSW + semantic_cache) — skip LLM if score ≥ 0.75
        ├─ trace_orchestrator (RAG_TRIALS → LLM_TOOLS → verify → resolve)
        ├─ Ollama LLM qwen3.6 → AnalystAdvisory schema (WHAT/WHO/WHY/HOW-TO/Forecast)
        ├─ CRAT write (fail-closed) ──→ kafka: omni-audit-chain
        └─→ kafka: omni-actions (SUGGEST_REMEDIATION | HITL_PENDING | EXECUTE_MUTATE)

kafka: omni-actions
  └─→ omni-executor (kafka_actions_loop)
        ├─ rollback_executor (pre-snapshot + auto-rollback)
        ├─ SandboxManager (OpenSandbox HTTP → policy gate)
        └─→ kafka: omni-action-feedback

kafka: omni-action-feedback
  └─→ omni-analyst (feedback loop + hot cache)
  └─→ omni-kpi-collector (ZADD rolling 24h)
```

## Remote Agent Pipeline (External Hosts)

```
Linux host / VM
  └─ src/remote_agent/agent.py (collectors: system, k8s, database, logs, storage)
        └─ OmniEmitter → POST /agent/push (omni-gateway)
              └─ routes/agent_push.py → kafka: omni-diagnostic-evidence
                    └─ workers/remote_agent_pipeline.py
                          ├─ cluster/triage/research/learn stages
                          └─ Telegram notify (critical/high tier)
```

## 4 Diagnostic Lanes

| Lane | ID | Evidence Source | Trigger |
|------|----|-----------------|---------|
| Resource | `SYS_RESOURCE` | 3σ z_cpu/z_mem via Prometheus | ThreeSigmaGate \|z\| > 3.0 |
| State fail | `SYS_HARD_FAIL` | K8s SDK probes (pod_status, events) | AlertManager → prober |
| App HTTP | `APP_HTTP` | Loki log surge (5xx/429/401/403) | log_surge_probe sigma bypass |
| SIEM | `SIEM_SECURITY` | FinGuard incident stream | siem_bridge XREADGROUP |

## Component Roles (OMNI_WORKER_ROLE)

| Role | Active Loops |
|------|-------------|
| `prober` | kafka_alerts_loop · delayed_queue · circuit_breaker · telegram_polling |
| `analyst` | kafka_evidence_loop · kafka_action_feedback_loop · kpi_collector |
| `core` | deep_scout · forecast · baseline_snapshot · proactive |
| `executor` | kafka_actions_loop |
| `siem-bridge` | Redis XREADGROUP → kafka omni-alerts |
| `evidence-adapter` | Redis XREADGROUP → kafka omni-diagnostic-evidence |
| `hitl-dispatcher` | omni-hitl-pending → FinGuard HITL API |
| `full` | all loops — active monolith (`omni-fullstack`, replicas=1); split-role pods scaled to 0 |

## Security Invariants

```
OMNI_AUTO_EXECUTE_ENABLED=false   → fail-closed (SUGGEST_REMEDIATION only)
CRAT write_audit_block()          → fail-closed before any Telegram/action emit
Executor RBAC                     → NEVER cluster-admin
kafka_evidence_loop               → auto_offset_reset="earliest" (do not change)
omni-audit-chain                  → log-compacted, key=seq (chain_writer.py)
```
