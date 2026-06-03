<!-- Generated: 2026-05-22 | Redis Stack + Kafka | Token estimate: ~800 -->

# Data — Omni SRE

## Primary Data Store: Redis Stack

### HNSW Vector Collections (Redis Search, 768-dim nomic-embed-text)

| Index | Key Pattern | Purpose |
|-------|-------------|---------|
| `idx:itops_sop_ledger` | `doc:itops_sop_ledger:{uuid}` | SOP advisory vectors |
| `idx:action_experience` | `doc:action_experience:{uuid}` | Resolved incident patterns |
| `idx:diagnostic_memory` | `doc:diagnostic_memory:{uuid}` | Diagnostic session memory |
| `idx:k8s_expert` | `doc:k8s_expert:{uuid}` | K8s knowledge base |
| `idx:errors` | `doc:errors:{uuid}` | Error pattern library |
| `idx:topology` | `doc:topology:{uuid}` | Cluster topology snapshots |

**Note:** Redis fields use `omni_payload` (not `payload`) to avoid `Document(payload=)` constructor clash.

### KPI Keys (rolling 24h ZADD, per-tenant)

```
omni:kpi:z:{tenant_id}:accepted        ZSET  score=timestamp, member=trace_id
omni:kpi:z:{tenant_id}:rejected        ZSET  score=timestamp, member=trace_id
omni:kpi:z:{tenant_id}:false_positive  ZSET  score=timestamp, member=trace_id
omni:kpi:detected:{tenant_id}:{lane}   String  MTTD measurement start timestamp
omni:kpi:resolved:{tenant_id}:{lane}   String  MTTR measurement end timestamp
```

**Note:** Migrated from flat keys (`omni:kpi:z:accepted`) to per-tenant pattern via `scripts/kpi_key_migrate.py`.

### Anomaly / Baseline

```
omni:baseline_snapshot              String (JSON)  600s TTL  3σ z_cpu/z_mem snapshot
3sigma:metric:{id}                  List           3600s TTL Rolling window (window=100 samples)
omni:proof_of_fault:window:{trace}  String         600s TTL  Sigma observation window counter
omni:sigma:config:{ns}:{dep}        Hash           14d TTL   Per-workload sigma config (auto-calibrated)
omni:maint:{ns}:{dep}               String         —         Maintenance window flag (suppresses alerts)
```

### CRAT Audit Chain

```
audit_chain:blocks                       List   ∞       SHA-256 chained blocks (JSON, Ed25519 signed)
audit_chain:head_hash                    String ∞       Current chain head SHA-256
audit_chain:seq                          String ∞       Monotonic block counter (Kafka compact key)
omni:crat:llm_reason:{trace}:{step}      String 86400s  Raw LLM reasoning text ref
```

### RAG / Cache

```
omni:rag:sop                      Hash   ∞      Advisory training pairs (HLEN=1000 post-ingest)
omni:semantic_cache:{hash}        String 3600s  Near-exact advisory cache
omni:recall:negative:{id}         String 30d    Negative pattern signals (S2.4)
omni:autonomous:hot:{trace}       String 24h    Verified success → fast path cache
```

### Trace / Orchestration

```
omni:trace_orchestrator:{trace_id}      String  24h  Phase state (RAG_TRIALS→LLM_TOOLS→verify→resolve)
omni:tenant:{tenant_id}:rate:{key}      String  —    Per-tenant rate limit bucket
session_state:{chat_id}                 String  —    Telegram session short-term memory
omni:autonomy:policy:{tenant_id}        Hash    —    AutonomyPolicyStore rules
```

### Remote Agent

```
omni:remote:agent:{agent_id}      Hash   300s  Agent registration + last_seen heartbeat
omni:remote:metrics:{agent_id}    Hash   300s  Latest collected metrics snapshot
```

### Heartbeat / Worker State

```
omni:hb:{worker_role}:{pod_id}    String  60s  Worker heartbeat (health_server pushes)
```

## Kafka Topics (Append Log)

| Topic | Partitions | Retention | Consumers |
|-------|-----------|-----------|-----------|
| `omni-alerts` | 1 | default | omni-prober-alerts |
| `omni-diagnostic-evidence` | 1 | default | omni-analyst-evidence |
| `omni-actions` | 1 | default | omni-executor-actions |
| `omni-action-feedback` | 1 | default | omni-analyst-action-feedback · omni-kpi-collector |
| `omni-hitl-pending` | 1 | default | omni-hitl-dispatcher |
| `omni-audit-chain` | 1 | **compacted** (key=seq) | archival |
| `omni-dlq` | 1 | default | omni-dlq-archiver |
| `omni-siem-raw` | 6 | default | brain-go consumer |
| `omni-siem-incidents` | 6 | default | omni-evidence-adapter |
| `omni-hitl-decisions` | 3 | default | hitl-dispatcher |

## Training Data (Flat Files)

```
data/rag_training/omni_sop_samples.jsonl   1000 advisory pairs (250 × 4 lanes)
data/sop/sop_templates.yaml                SOP seed templates (pgvector_health entries stale — skip)
config/diagnostic_matrix.yaml             Alert → lane mapping rules
config/incident_training_matrix.yaml      Incident training registry
```

## No SQL Database
PostgreSQL removed. All persistence is Redis Stack + Kafka append log.
