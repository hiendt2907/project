<!-- Generated: 2026-05-22 | Files scanned: 270 Python | Token estimate: ~1000 -->

# Backend — Omni SRE

## Entry Points

| Service | Entry | Role |
|---------|-------|------|
| Worker | `src/workers/__main__.py` → `omni_worker.main()` | All async loops per OMNI_WORKER_ROLE |
| Gateway | `src/gateway/api.py` (FastAPI `app`) | HTTP ingress, no worker imports |
| Analyst svc | `src/services/analyst/__main__.py` | Standalone analyst runner |

## Gateway Routes (FastAPI, auth: X-API-Key)

```
GET  /healthz                              → liveness check
GET  /metrics                              → Prometheus exposition
POST /webhook/prometheus                   → KafkaBus.send_dict("omni-alerts")
POST /forecast/matrix                      → forecast matrix (AnalystAdvisory schema)
GET  /metrics/circuit_breaker              → circuit breaker state

# Mounted routers (all require API key unless noted)
GET/POST /kpi/summary|trend|clusters|prompt-ab  → routes/kpi.py → app.state.redis ZADD
GET      /playbooks · /{id} · /{id}/state       → routes/playbooks.py
POST     /playbooks/{id}/approve|reject         → routes/playbooks.py → FinGuard HITL API
GET/POST /autonomy/policy|rule|history|reset    → routes/autonomy.py → AutonomyPolicyStore
GET      /compliance/export                     → routes/compliance.py → CRAT chain
GET      /siem/overview                         → routes/siem.py → CRAT + Redis
GET      /agents · /agents/remote               → routes/agents.py → Redis heartbeat keys
GET      /agents/remote/{id}/logs               → routes/agents.py
DELETE   /agents/remote/{id}                    → routes/agents.py
POST     /agent/webhook                         → routes/agent_webhook.py
POST     /agent/push                            → routes/agent_push.py (own auth, RemoteAgent)
```

## Worker Roles & Active Loops

```
prober   → kafka_alerts_loop · delayed_queue · circuit_breaker · telegram_polling
analyst  → kafka_evidence_loop · kafka_action_feedback_loop · kpi_collector
core     → deep_scout · forecast · baseline_snapshot · proactive
executor → kafka_actions_loop
```

## Key Worker Modules (src/workers/)

```
omni_worker.py                Main entry — wires loops per OMNI_WORKER_ROLE
evidence_consumer.py          Batch evidence → RAG gate → LLM advisory → CRAT → actions
advisory_analyst_handler.py   LLM chat + retry → AnalystAdvisory + CRAT write
analyst_agentic_loop.py       N-step ReAct planner (EXECUTE_MUTATE path)
autonomous_feedback_loop.py   action-feedback → hot cache | replan
diagnostic_dispatcher.py      Alert → probe plan (smart_tier1/2) selection
diagnostic_k8s_clinical.py    K8s SDK probes: pod_status, pod_metrics, events
baseline_snapshot.py          3σ rolling window; writes omni:baseline_snapshot
kpi_metrics.py                KPI ZADD rolling 24h (omni:kpi:z:{tenant}:*)
health_server.py              HTTP :8090 passive health/readiness server
remote_agent_pipeline.py      RemoteAgent evidence → cluster/triage/research/learn/notify
rollback_executor.py          Pre-snapshot + auto-rollback for FULL_AUTO mutations
hitl_dispatcher.py            HITL approval flow (FinGuard API polling + Slack fallback)
metrics_exporter.py           Prometheus /metrics thread server :9090
siem_bridge.py                FinGuard Redis XREADGROUP → kafka omni-alerts
telegram_advisory_emitter.py  AnalystAdvisory → Markdown → Telegram (lane badge [RESOURCE]/[SIEM]/etc.)
llm_context_budget.py         build_llm_options() helper; num_ctx/temperature config
request_trace.py              ContextVar trace_id; structured request logging
```

## Key Package Modules (src/pkg/)

```
reasoning/analyst_advisory_schema.py          AnalystAdvisory (WHAT/WHO/WHY/HOW-TO/Forecast)
reasoning/deterministic_mutate_from_evidence.py  Probe-based planner without LLM
reasoning/sanitize.py                         Evidence field sanitizer (injection patterns)
rag/gate.py                                   RAG recall gate (score threshold 0.75)
rag/embed_utils.py                            truncate_for_embedding() (nomic-embed-text 512-token)
trace_orchestrator/__init__.py                Trace state machine (RAG_TRIALS→LLM_TOOLS→verify)
trace_orchestrator/state.py                   Redis-backed TraceOrchestratorState
trace_orchestrator/candidates.py              merge_ranked_candidate_rows() for RAG/playbook
autonomy/transform.py                         Evidence context budget; clamp_evidence_text()
autonomy/gate.py                              AutonomyGate: FULL_AUTO vs SUGGEST_ONLY decision
autonomy/llm_contract.py                      HighLevelRemediationPlan strict JSON contract
clustering/incident_cluster.py                Embedding-based incident clustering (S3.1)
prompt_optimizer/ab_test.py                   A/B prompt variant assignment (S3.3)
```

## Services (src/services/)

```
audit_ledger/chain_writer.py    SHA-256 hash-chain + Ed25519 signing → Redis + Kafka (fail-closed)
audit_ledger/signer.py          Ed25519 PEM key signing (SOX §404, PCI-DSS v4.0)
audit_ledger/verifier.py        Block integrity verification
playbook/matcher.py             PlaybookMatcher: SIEM label → pre-approved Playbook
playbook/state_machine.py       StepStateMachine: Redis-backed step execution (7 statuses, TTL 2h)
learning_promoter/promoter.py   SOP promotion after success threshold (omni:rag:sop:*)
evidence_adapter/worker.py      AdapterGeneratorWorker: Redis Stream → Kafka diagnostic-evidence
evidence_adapter/siem_adapter.py  SIEM incident → Omni evidence envelope
```

## Remote Agent (src/remote_agent/)

```
agent.py                       Main async collection loop → OmniEmitter → /agent/push
emitter.py                     OmniEmitter: registration + evidence submission with retry
evidence.py                    build_envelope(): builds diagnostic evidence payload
collectors/system.py           CPU / memory / disk / load avg with anomaly detection
collectors/k8s.py              K8s pod status + resource health + events
collectors/logs.py             Tail log files; count ERROR/CRITICAL in sliding window
collectors/database.py         MySQL health + ProxySQL stats (read-only, radmin user)
collectors/storage.py          Disk / volume usage metrics
settings.py                    AgentSettings (env-based config)
```

## Execution (src/execution/)

```
manager.py    SandboxManager: OpenSandbox HTTP client + Kafka audit + policy denylist
policy.py     check_sandbox_command(): strict denylist patterns + promotion allowlist
experience.py  Sandbox result → lesson generation → RAG action_experience
```

## RAG Stack (src/rag/)

```
redis_vector_store.py   Redis Stack HNSW (768-dim, omni_payload field)
                        COLLECTION_* constants: SOP, errors, topology, k8s_expert, etc.
semantic_cache.py       Near-exact advisory cache (TTL 3600s)
sop_ledger.py           SOP HNSW collection
```

## Anomaly Detection (src/anomaly/)

```
three_sigma.py        ThreeSigmaGate: window=100, anomaly |z| > 3.0
forecast.py           linear_forecast_horizon() + oom_risk_from_series()
sigma_calibrator.py   Per-workload auto-calibration from 7-day Prometheus history
prophet_forecast.py   Prophet-based long-horizon forecast
```

## Supporting Modules

```
src/messaging/kafka_bus.py          Kafka producer helpers + envelope validation
src/metrics/prometheus_dataframe.py Prometheus query_range JSON → pandas DataFrame
src/observability/normalize.py      PII/secrets redaction + canonical query for RAG embed
src/llm/vllm_client.py              VLLMClient: async OpenAI-compatible Ollama wrapper
src/llm/factory.py                  build_llm_client() factory
src/knowledge/pipeline.py           Vendor knowledge ETL: fetch → clean → chunk → embed
src/sre/watchdog.py                 OmniWatchdog: autonomous diagnostic + self-healing agent
src/visualization/chart_bytes.py    Matplotlib line chart → PNG bytes (in-memory)
```

## Kafka Topics

```
omni-alerts (p=1)              omni-diagnostic-evidence (p=1)    omni-actions (p=1)
omni-action-feedback (p=1)     omni-hitl-pending (p=1)           omni-audit-chain (p=1, compacted)
omni-dlq (p=1)                 omni-siem-raw (p=6)               omni-siem-incidents (p=6)
omni-hitl-decisions (p=3)
```
