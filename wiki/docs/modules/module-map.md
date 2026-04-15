# Module mapping (`src/`)

High-signal map of what major modules **do**. Paths are relative to repo root.

## Gateway (isolated ingress)

| Module | Role |
|--------|------|
| `src/gateway/api.py` | FastAPI app: `/healthz`, `/metrics`, `/webhook/prometheus`, `/metrics/circuit_breaker`. Produces Kafka alerts; rate limit + Redis circuit breaker. |
| `src/gateway/trace_context.py` | Trace ID for gateway logging. |

## Workers (Kafka loops, probes, LLM orchestration)

| Module | Role |
|--------|------|
| `src/workers/omni_worker.py` | Main entry: schedules role-specific background tasks (`_worker_background_tasks`). |
| `src/workers/settings.py` | Pydantic settings: Kafka topics, Ollama URL, proof-lane flags, baseline PromQL, log-surge params, env mode. |
| `src/workers/alert_to_event.py` | Translates alert payloads into internal event structures for probing. |
| `src/workers/diagnostic_dispatcher.py` | Selects and runs diagnostic probes for an anomaly event. |
| `src/workers/diagnostic_probe_registry.py` | Registry mapping probe names → async functions (K8s clinical, Prom, Redis ping, security probes, etc.). |
| `src/workers/diagnostic_k8s_clinical.py` | K8s-focused probes: pod status, logs, events, metrics, quotas. |
| `src/workers/diagnostic_evidence.py` | Structures for probe runs / evidence records. |
| `src/workers/diagnostic_mapping.py` | Maps alerts/symptoms to probe lists and policy. |
| `src/workers/evidence_batch.py` | Batches evidence until flush thresholds for analyst. |
| `src/workers/evidence_consumer.py` | Consumes `omni-diagnostic-evidence`: RAG gate, **proof-of-fault** (`_proof_of_fault_gate`), LLM/deterministic mutate emit. |
| `src/workers/evidence_mutate_emit.py` | Builds and emits execute-mutate actions; stores autonomous trace context in Redis. |
| `src/workers/log_surge_probe.py` | **App-log lane:** Loki `query_range`, parses access/JSON lines for sustained 5xx; `evaluate_log_surge_sigma_bypass`. |
| `src/workers/baseline_snapshot.py` | Periodic health manifest: Prom metrics, **z_cpu/z_mem**, `dr`, CHS/wide incident; Redis snapshot key. |
| `src/workers/kafka_actions_consumer.py` | Executor: consumes `omni-actions`, runs mutations. |
| `src/workers/autonomous_execute.py` | Execute path, allowlist, publish feedback after mutate. |
| `src/workers/autonomous_feedback_loop.py` | Consumes `omni-action-feedback` for analyst / learning. |
| `src/workers/post_mutate_sdk_verify.py` | Post-mutate re-probe verify when enabled. |
| `src/workers/analyst_agentic_loop.py` | LLM ReAct / agentic mutate planning boundaries. |
| `src/workers/metrics_exporter.py` | Prometheus metrics for worker behavior. |

## Reasoning & policy

| Module | Role |
|--------|------|
| `src/pkg/reasoning/incident_matrix_profile.py` | Loads YAML matrix; **`resolve_proof_lane`**, `workload_profile_for_alert`, `is_api_web_workload`. |
| `src/pkg/reasoning/evidence_signals.py` | **`critical_evidence_present`** and related signal extraction. |
| `src/pkg/reasoning/deterministic_mutate_from_evidence.py` | Deterministic mutate plans from evidence when LLM is skipped or replanning. |
| `src/pkg/reasoning/diagnostic_policy.py` | Invariants, reasoning chain payloads. |
| `src/pkg/rag/gate.py` | RAG retrieval gate before LLM-heavy paths. |

## Anomaly / forecasting

| Module | Role |
|--------|------|
| `src/anomaly/three_sigma.py` | **`ThreeSigmaGate`**: Redis rolling list, z-score anomaly if **\|z\| > 3** (see architecture doc — distinct from baseline PromQL z). |
| `src/anomaly/forecast.py`, `prophet_forecast.py` | Time-series helpers (used by core/forecast loops as configured). |

## Messaging & RAG

| Module | Role |
|--------|------|
| `src/messaging/kafka_bus.py` | Kafka helpers / producer patterns. |
| `src/rag/pgvector_store.py` | pgvector collections (expert, action_experience, SOP, etc.). |

## Package root

| Module | Role |
|--------|------|
| `src/pkg/autonomous_actions.py` | Action envelope schemas for executor/feedback. |
| `src/pkg/autonomy/*` | Autonomy lifecycle, LLM contract, transforms (GIGO). |

## Prober package

| Module | Role |
|--------|------|
| `src/prober/clinical.py` | Clinical-style probe helpers (thin layer; heavy lifting in `workers/diagnostic_*`). |
