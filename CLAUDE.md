# CLAUDE.md

**Omni** — async-first multi-agent SRE automation for K8s. Ollama diagnoses via 3 evidence lanes (state, app_log, metrics); split Kafka pipeline executes remediation.

## DIAGNOSTIC FLOWS (3 lanes + SIEM)

### Lane 1 — Resource (time series)
`baseline_snapshot.py` computes rolling z-score (3σ) for CPU/mem each tick → stored in Redis (`omni:baseline_snapshot`).
`ThreeSigmaGate` (`src/anomaly/three_sigma.py`): `window=100`, `ttl=3600s`, anomaly when `|z| > 3.0`.
`_proof_of_fault_gate` reads `z_cpu`/`z_mem` from Redis snapshot; injects `3-SIGMA RESOURCE BASELINE` block into advisory evidence text.
`ImpactForecast` schema: 5 horizons `1h/3h/6h/12h/24h`, `method=linear_extrapolation`, `basis="prometheus predict_linear"`.
`src/anomaly/forecast.py`: `linear_forecast_horizon()` + `oom_risk_from_series()`.

### Lane 2 — System errors (WHAT/WHO/WHY/HOW-TO + forecast)
`AnalystAdvisory` schema (`src/pkg/reasoning/analyst_advisory_schema.py`):
- **WHAT**: `root_cause` — one sentence, concrete scope (ns/workload/pod)
- **WHO**: `affected_workload` — namespace/deployment
- **WHY**: `verification_steps[].rationale` — proves/disproves root cause
- **HOW-TO**: `proposed_remediation[]` — safe steps, `approval_required` flag
- **Forecast**: `ForecastTimeline` — 5 timeframes, method=linear_extrapolation|kill_chain|heuristic
Advisory system prompt: bottom-up L1→L4 layered diagnosis (os_baremetal → network → kubernetes → prometheus).

### Lane 3 — Business errors (HTTP status classes)
`log_surge_probe.py` (`evaluate_log_surge_sigma_bypass`):
- **5xx** (500-504): server error → sigma bypass OK
- **rate_limit** (429): rate limiting → sigma bypass OK, `dominant_error_class=rate_limit`
- **client_abort** (499): nginx client abort → informational only, NOT sigma bypass
- **auth_failure** (401/403): auth error → sigma bypass OK, `dominant_error_class=auth_failure`
`count_access_errors()` → `AccessErrorCounts` with per-class histogram.
`classify_http_status(status: int) → ErrorClass`.

### Lane 4 — Smart-SIEM (FinGuard incidents)
`_siem_diagnosis_from_batch()` produces structured output:
- **WHAT**: incident category + description
- **WHO**: namespace, tenant, source_ip, incident_id
- **WHY**: `_SIEM_CATEGORY_WHY[category]` — root cause basis per attack type
- **HOW-TO**: `_SIEM_CATEGORY_STEPS[category]` — kubectl investigation steps
- **Forecast**: `_siem_forecast_timeline(category, severity)` → 5 horizons kill-chain heuristic
`_notify_siem_telegram()` includes forecast +1h/+3h/+6h/+12h/+24h severity escalation in Telegram card.
`render_advisory_to_telegram(ctx, advisory, chat_id, *, lane_label)` — pass `lane_label` from `resolve_proof_lane(batch)` to stamp `[RESOURCE]`/`[STATE_FAIL]`/`[APP_LOG]`/`[SIEM]` badge in header.
`_SIEM_FORECAST`: per (category × severity) timeline — ddos, malware, data_exfil, k8s_threat, auth_failure, lateral_movement, network_anomaly.

## ARCHITECTURE & FLOW

### End-to-End Pipeline

```
[FinGuard Redis] stream:actionable_incidents
    → omni-siem-bridge → kafka: omni-alerts

[SIEM raw events] stream:siem_evidence_raw
    → omni-evidence-adapter → kafka: omni-diagnostic-evidence

[Prometheus / proactive PromQL]
    → omni-prober (role=prober)
        → run_diagnostic_pipeline + temporal_evidence_collector
        → kafka: omni-diagnostic-evidence

kafka: omni-diagnostic-evidence
    → omni-analyst (role=analyst)
        → RAG gate (Redis HNSW + semantic_cache)
        → Ollama LLM (qwen2.5-coder:7b, num_ctx=8192)
        → AnalystAdvisory schema (read-only)
        → AdvisoryModeKillSwitch (OMNI_AUTO_EXECUTE_ENABLED=false → fail-closed)
        → CRAT: write_audit_block() → Redis chain + kafka: omni-audit-chain  ← FAIL-CLOSED
        → Advisory Mode (OMNI_SIEM_SUGGEST_ONLY=true):
            SUGGEST_REMEDIATION → kafka: omni-actions
            telegram_advisory_emitter → Telegram Bot
        → Mutation Mode (explicit enable only):
            EXECUTE_MUTATE → kafka: omni-actions
            OR HITL_PENDING → kafka: omni-hitl-pending

kafka: omni-hitl-pending → omni-hitl-dispatcher → FinGuard HITL API
    APPROVED → kafka: omni-actions
    REJECTED → kafka: omni-action-feedback

kafka: omni-actions → omni-executor (role=executor) → kafka: omni-action-feedback

kafka: omni-action-feedback → omni-analyst (re-evaluation cycle)
```

### Component Roles (OMNI_WORKER_ROLE)

| Role | Active loops |
|---|---|
| `prober` | kafka_alerts_loop, delayed_queue, circuit_breaker, telegram_polling |
| `analyst` | kafka_evidence_loop, kafka_action_feedback_loop, **kpi_collector** |
| `core` | deep_scout, forecast, baseline_snapshot, proactive |
| `executor` | kafka_actions_loop |
| `full` | all (legacy monolith) + kpi_collector |
| `siem-bridge` | Redis XREADGROUP → kafka omni-alerts |
| `evidence-adapter` | Redis XREADGROUP → kafka omni-diagnostic-evidence |
| `hitl-dispatcher` | omni-hitl-pending → FinGuard HITL API |
| `gateway` | FastAPI HTTP → kafka omni-alerts (separate image) |

---

## OBSERVABILITY & QUALITY (2026-05-10)

### Self-monitoring — Worker Health Server
`src/workers/health_server.py` — thread-based HTTP :8090, passive model (states pushed by async loop, không pull từ thread).
- `GET /healthz` → `{"status":"ok|degraded|unhealthy","checks":{...}}` — 503 khi unhealthy
- `GET /readyz` → 200 nếu status != "unhealthy" (connectivity-based, NOT message-based)
- Checks: `kafka_lag` (>1000 = unhealthy), `redis_ping`, `llm_up` (0 = degraded), `last_message_age` (>600s = unhealthy)
- States pushed bởi `observability_metrics_loop()` via `update_check_state(name, status, detail)`
- `record_message_processed()` gọi từ `kafka_evidence_loop` (omni_worker.py) và handlers.py (Telegram path)
- K8s: readinessProbe `/readyz` port 8090 `initialDelaySeconds:30`; livenessProbe `/healthz` port 8090 `initialDelaySeconds:90`

`k8s/monitor/prometheus-rules-omni-health.yaml` — 7 alerts mới (không sửa file cũ):
`OmniWorkerStalled` · `OmniWorkerHealthDegraded` · `OmniWorkerHealthUnhealthy` · `OmniRedisConnectionLost` · `OmniLLMSustainedDown` · `OmniAdvisoryAcceptanceRateLow` · `OmniFalsePositiveRateHigh`

### Business KPI Dashboard
`src/workers/kpi_metrics.py` — consumer group `omni-kpi-collector` trên `omni-action-feedback`. Rolling 24h window via ZADD+ZREMRANGEBYSCORE (KHÔNG dùng INCR — tránh overflow sau 24h).
- Redis keys (per-tenant): `omni:kpi:z:{tenant_id}:accepted|rejected|false_positive` — migrated from flat keys via `scripts/kpi_key_migrate.py`
- MTTD/MTTR: `omni:kpi:detected:{tenant_id}:{lane}`, `omni:kpi:resolved:{tenant_id}:{lane}` (per lane: SYS_RESOURCE/SYS_HARD_FAIL/APP_HTTP/SIEM_SECURITY)
- Outcomes mapped: `success|APPROVED|verified` → accepted; `rejected|REJECTED` → rejected; `fail|executor_fail` → false_positive

`src/gateway/routes/kpi.py` — `GET /kpi/summary` + `GET /kpi/trend?window=1h|6h|24h|7d`. Require `request: Request` (FastAPI injection), NOT `request: Any`.
- Mounted via `app.include_router(_kpi_router, dependencies=[_Depends(_require_api_key)])` trong `gateway/api.py`
- `app.state.redis` set trong lifespan — gateway cần redis để serve KPI

`ui/app/kpi/page.tsx` — read-only KPI dashboard: 4 stat cards, acceptance/false-positive pie charts, lane resolution bar chart.
`ui/app/api/kpi/route.ts` — Next.js proxy với mock fallback khi gateway không available.

### Advisory Quality Benchmark
`tests/benchmarks/advisory_golden/case_001–010.json` — 10 golden cases từ post-mortems (missing ConfigMap, Redis OOM, Kafka lag, 5xx surge, DDoS, normal CPU, ImagePullBackOff, auth surge, LLM down, CRAT integrity).
`tests/benchmarks/run_advisory_benchmark.py` — 100pt scoring rubric: verdict(30) + keywords(20) + no-hallucination(20) + remediation(15) + verification_steps(15). Pass threshold: 70/100.
`tests/benchmarks/test_advisory_quality.py` — pytest wrapper: 10 schema validation tests (luôn pass) + 1 live LLM test (skip khi không có OMNI_OLLAMA_BASE_URL).
`make benchmark-advisory` — chạy benchmark, informational (không block CI).

### New metrics in metrics_exporter.py
`omni_worker_last_message_age_seconds` · `omni_health_check_status{check_name}` · `omni_kpi_mttd_seconds{lane}` · `omni_kpi_mttr_seconds{lane}` · `omni_kpi_advisory_acceptance_rate` · `omni_kpi_false_positive_rate` · `omni_kpi_incidents_total{lane,outcome}` · `omni_advisory_benchmark_score{model,case_id}` · `omni_advisory_benchmark_pass_rate`

### docs/CODEBASE.md
723-line codebase map: module index (1 dòng/file ~400 files), Kafka topic map, Redis key map, 4 critical data flows, worker role map, dependency graph.

### Lane 2 (SYS_HARD_FAIL) — OS State Machine
`src/workers/os_state_validator.py` — deterministic check chạy trong Omni analyst (không phải remote agent).
- `compare_alert_claim_to_os_state(by_probe)` → contrast string nếu probe PASSED mâu thuẫn với SYS_HARD_FAIL alert, None nếu confirm thật hoặc thiếu data.
- Probes: `systemd_units` (systemctl), `service_haproxy`/`service_haproxy_prom`, `disk_usage` (df -h/i), `storage_nfs`, `mysql_health`, `proxysql_health`.
- Gọi trong `evidence_consumer.py` sau SDK contrast, trước advisory LLM — source=`OS_STATE_CONTRAST`.
- Remote agent chỉ gửi data; Omni tự validate không cần remote agent tự classify.

`data/rag_training/sys_hard_fail_os_advisory_pairs.jsonl` — 40 pairs OS-level: systemd (15), disk/NFS (8), MySQL/ProxySQL (4), HAProxy (3), OOM/kernel (3), network (3), node-level K8s (4).
- Ingest: `PYTHONPATH=src .venv/bin/python src/training/advisory_ingest.py --path data/rag_training/sys_hard_fail_os_advisory_pairs.jsonl --redis-url redis://localhost:16379/0`
- Regenerate: `python3 scripts/gen_sys_hard_fail_rag.py`

---

## INVARIANTS

- Async-only: `asyncio`, `kubernetes-asyncio`, `redis[hiredis]`, `aiokafka`. No subprocess for K8s.
- `trace_id` end-to-end via `request_trace.py` push/pop.
- `OMNI_ENV_MODE=lab|prod` — enforced by `validate_env_mode_gate.py`.
- `OMNI_LLM_NUM_CTX` default 8192 (raised from 4096 in S2.1). Use `build_llm_options(ctx, temperature=0.1)` from `workers.llm_context_budget` — never inline `getattr(getattr(ctx, "settings", None), "llm_num_ctx", 4096)` again.
- `src/gateway/` must NOT import worker/executor/prober.
- Mutations only via executor; analyst is read-only.
- Containers: `USER appuser` uid 10001.
- Secrets: env + K8s Secrets only; gitleaks CI gate.
- **Executor RBAC: NEVER cluster-admin.**
- `OMNI_AUTO_EXECUTE_ENABLED=false` — master kill-switch (fail-closed).
- **CRAT Fail-Closed**: `write_audit_block()` MUST succeed before any Telegram emit or action dispatch. Failure aborts the transaction.
- `kafka_evidence_loop` uses `auto_offset_reset="earliest"` — analyst recovers messages that arrive during consumer-group rebalance on pod restart. DO NOT change to `latest`.
- `omni-audit-chain` Kafka topic requires a message key (compact policy); `chain_writer.py` passes `key=str(seq).encode()`.
- E2E harness `scripts/verify_e2e_crat_pipeline.py` auto-resolves OrbStack ClusterIPs via kubectl; override with `E2E_KAFKA_BOOTSTRAP` / `E2E_REDIS_MA_URL` / `E2E_REDIS_FG_URL`.

### CRAT — Cryptographic Regulatory Audit Trail (SOX §404, PCI-DSS v4.0)
`src/services/audit_ledger/` — SHA-256 hash-chaining + Ed25519 signing.
- Block N hash includes Block N-1 hash → retrospective tampering detectable.
- Chain: Redis `audit_chain:blocks` / `audit_chain:head_hash` + Kafka `omni-audit-chain`.
- `OMNI_AUDIT_PRIVATE_KEY_PATH` — Ed25519 PEM private key (K8s Secret mount). Unset = unsigned (lab only).
- Event types: `ADVISORY_DECISION`, `ADVISORY_DISPATCHED`, `MUTATION_TRAPPED`, `HITL_DECISION`, `ROLLBACK_EXECUTED`, `SOP_PROMOTED`.
- `llm_reasoning_hash` + `llm_reasoning_ref` stored in `ADVISORY_DECISION` block (S1.4).
- Raw LLM reason stored at `omni:crat:llm_reason:{trace}:{step}` (TTL=86400, S1.4).

### RBAC
- `omni-worker` SA: pods/log, events read in `multi-agent` only. **No Secrets.**
- `omni-temporal-prober-role` ClusterRole: get/list/watch pods, nodes, deployments, services. No write, **no Secrets**.

### Bottom-Up Diagnostics L1→L4
L1=os_baremetal · L2=network · L3=kubernetes (read-only) · L4=prometheus

### Diagnostic Policy Invariants
`INV_NO_RESTART_ON_BROKEN_SPEC` · `INV_READ_BEFORE_MUTATE` · `INV_NAMESPACE_ISOLATION` · `ERR_REA_NO_PHYSICAL_PROOF` · `ERR_GOV_UNAUTHORIZED_MUTATION`

---

## INFRASTRUCTURE

- **K8s**: OrbStack, namespace `multi-agent`; `finguard-customer` for HITL API
- **Python**: async-first, Pydantic settings (`WorkerSettings`, `OMNI_` prefix)
- **LLM**: Ollama `VLLMClient` — `qwen2.5-coder:7b` (active, all roles) + `nomic-embed-text:latest` (768-dim embed); `llm_num_ctx=8192` (default, env `OMNI_LLM_NUM_CTX`). `qwen3.6` available on host but NOT active.
- **RAG**: Redis Stack HNSW `redis_vector_store.py` + `semantic_cache.py`
- **Kafka**: `aiokafka`; `KafkaBus.send_dict(topic, dict)`
- **Tests**: pytest `asyncio_mode=auto` `pythonpath=src`; `FakeAsyncRedis(decode_responses=True)`; `_KafkaCapture.send_dict(topic, envelope)`. Context: `SimpleNamespace(redis, kafka, settings)`.

## Key Dirs

`src/workers/` · `src/gateway/` · `src/pkg/reasoning/` · `src/pkg/executor/` · `src/pkg/clustering/` · `src/pkg/temporal/` · `src/pkg/prompt_optimizer/` · `src/rag/` · `src/prober/` · `src/services/{analyst,playbook,evidence_adapter,audit_ledger,learning_promoter}/` · `src/llm/` · `src/messaging/` · `src/anomaly/{three_sigma,forecast,prophet_forecast,sigma_calibrator}.py` · `k8s/deployments/` · `smart-siem/omni/siem/{brain-go,agent,bff,contracts}/` · `tests/` · `tests/integration/`.

## Commands

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
.venv/bin/python -m pytest tests/integration/ -q
make autonomy-gate
make docker-worker docker-gateway
make deploy-worker deploy-gateway deploy-kafka deploy-ollama
make ensure-kafka-topics deploy-siem-stack
make e2e-proactive e2e-incident-matrix lab-nginx-cpu rag-hot-sync
NS=multi-agent make omni-death-loop  # or: bash scripts/omni_dev_death_loop.sh
# E2E scripts (gateway/matrix/nginx CPU): Makefile sets NS=multi-agent; invoking scripts directly requires NS (no default).
make benchmark-advisory                        # advisory quality benchmark (informational)
OMNI_OLLAMA_BASE_URL=http://localhost:11434 make benchmark-advisory  # live LLM benchmark
make chaos-drill-rollback                      # S1.2: inject bad configmap → verify auto-rollback
curl localhost:8090/healthz                    # worker health check
curl localhost:8090/readyz                     # worker readiness check
curl localhost:8080/kpi/clusters               # S3.1: incident cluster stats
curl localhost:8080/kpi/prompt-ab              # S3.3: A/B prompt variant stats
```

CI order: build → rollout → unit → E2E.

## Env

`OMNI_WORKER_ROLE` (prober|analyst|core|executor|full) · `OMNI_ENV_MODE` (lab|prod) · `OMNI_KAFKA_BOOTSTRAP_SERVERS` · `OMNI_REDIS_URL` · `OMNI_OLLAMA_BASE_URL` · `OMNI_AUDIT_PRIVATE_KEY_PATH` · `OMNI_ADVISORY_NUM_PREDICT` (optional; default 1024).
`OMNI_LLM_NUM_CTX` (default 8192) · `OMNI_PROACTIVE_LLM_NUM_CTX` (default 4096).
`OMNI_AUTO_ROLLBACK_ENABLED` (default true) · `OMNI_ROLLBACK_SNAPSHOT_TTL_SEC` (default 3600).
`OMNI_HITL_FALLBACK_CHANNEL` (slack|none, default none) · `OMNI_HITL_FALLBACK_WEBHOOK_URL` · `OMNI_HITL_ESCALATION_TIMEOUT_SEC` (default 900).
`OMNI_SOP_AUTO_PROMOTE_ENABLED` (default true) · `OMNI_SOP_PROMOTION_MIN_SUCCESS` (default 3).
`OMNI_FORECAST_PROACTIVE_INTEGRATION_ENABLED` (default true).
Postgres removed — RAG on Redis Stack HNSW + semantic cache.

**Smart-SIEM (brain-go) Kafka transport env:**
`BRAIN_TRANSPORT=redis|kafka` (default=redis) · `BRAIN_KAFKA_BOOTSTRAP` · `BRAIN_KAFKA_CONSUME_TOPIC` (omni-siem-raw) · `BRAIN_KAFKA_PRODUCE_TOPIC` (omni-siem-incidents) · `BRAIN_KAFKA_CONSUMER_GROUP`.
`SIEM_BRIDGE_DUAL_EMIT=true` → siem_bridge also publishes raw incident to `omni-siem-raw`.

**Kafka topics (Phase 2):** `omni-siem-raw` (partitions=6) · `omni-siem-incidents` (partitions=6) · `omni-hitl-decisions` (partitions=3).

## Tenant Isolation

Gateway auth: `X-API-Key` header checked via `_require_api_key()` in `src/gateway/middleware/auth.py`.
`OMNI_TENANT_APIKEYS` format: `tenant_id:key,tenant_id2:key2` (comma-separated).
Redis key prefix per tenant: `omni:tenant:{tenant_id}:` — rate limit buckets, KPI keys isolated per tenant.
`OMNI_GATEWAY_API_KEY` env (K8s Secret `omni-gateway-secret`) — master key for admin endpoints.
Multi-tenant KPI: `/kpi/summary?tenant=default` — filter by tenant label if set.

## RAG Training

Training data: `data/rag_training/omni_sop_samples.jsonl` — 1000 advisory pairs (250 × 4 lanes).
Ingest: `PYTHONPATH=src .venv/bin/python src/training/advisory_ingest.py --path data/rag_training/omni_sop_samples.jsonl --redis-url redis://localhost:16379/0`
Verify: `kubectl exec -n multi-agent redis-0 -- redis-cli HLEN "omni:rag:sop"` → should be ≥ 1000.
SOP YAML seed: `data/sop/sop_templates.yaml` (for sop_ingest.py — needs Ollama for embeddings).
Note: `pgvector_health` in YAML templates is stale (removed from TOOL_REGISTRY) — skip those templates.
Advisory ingest script: `src/training/advisory_ingest.py` (writes to `omni:rag:sop` hash, no embedding required).

## UI — Admin Dashboard & Operator Console (2026-05-20)

Admin Dashboard: `ui/app/admin/page.tsx` — SRE engineer view.
- Sections: System Health Bar (pod grid), KPI Live (acceptance/MTTD/MTTR), CRAT Audit Chain, Active Traces, Alert Injection Form, Tenant Management.
- Style: dark luxury, amber accents, monospace font, 6-column pod mini-cards.

Operator Console: `ui/app/operator/page.tsx` — on-call engineer view.
- Sections: Incident Feed (left-border lane colors), Advisory Panel (expandable verification steps), HITL Queue (countdown timer), Telegram Status.
- Style: dark, lane-colored borders, split-panel (incident list + detail).

Mock data: `ui/mocks/admin-mock.ts`, `ui/mocks/operator-mock.ts`.
Both pages use existing `/api/*` Next.js proxy routes — no new backend endpoints.

## Known Issues (2026-06-03)

1. **mktemp macOS**: `scripts/proactive_e2e.sh` creates `/tmp/e2e-gw-*.json` files that persist after test. Stale files may cause false E2E results — `rm /tmp/e2e-gw-*.json` before re-running.
2. **KPI old flat keys**: New environments must run `scripts/kpi_key_migrate.py` once to move legacy `omni:kpi:z:accepted` etc. to per-tenant keys. Lab: migrated 2026-05-21.

> Resolved 2026-06-03 audit: `pgvector_health` stale templates removed from `sop_templates.yaml` + `routing_policy.py`; split-role deployments/RBAC deleted (consolidated into `omni-fullstack-rbac.yaml`); `OMNI_LLM_NUM_CTX` default code-aligned to 8192; executor lab RBAC stripped of cluster-wide RBAC write verbs (anti self-escalation).

## Deployment State (2026-06-03)

Active pod: `omni-fullstack` (1/1 Running, `OMNI_WORKER_ROLE=full`) — sole worker. Split-role deployments/RBAC (analyst/prober/executor/core/worker) **deleted**; all Role/ClusterRole defs consolidated into `k8s/deployments/omni-fullstack-rbac.yaml`. `make deploy-worker` aliases `deploy-fullstack`.
- Kafka: 1 replica; consumers rebalance ~8s on pod restart.
- Redis: 1 replica; port-forward on 16379 for local access.
- Ollama: `host.orb.internal:11434` (OrbStack host), active model: `qwen2.5-coder:7b`, embed: `nomic-embed-text`.
- Secrets: `omni-audit-keys` (Ed25519, lab unsigned), `telegram-bot` (chat_id in K8s Secret, not in repo).
- OMNI_AUTO_EXECUTE_ENABLED=false (fail-closed, all lanes → SUGGEST_REMEDIATION only).
- RAG: `omni:rag:sop` HLEN=1000, re-ingested 2026-05-21 (omni_payload field rename applied).
- KPI keys: migrated to per-tenant pattern (`omni:kpi:z:default:*`) via `scripts/kpi_key_migrate.py`.

## Communication Style

- **Code first, always.** Viết code ngay, không hỏi lại.
- **Giải thích tối đa 100 chữ** — chỉ khi thật sự cần thiết.
- Tập trung vào: code, logic nghiệp vụ, xây dựng hệ thống.

# AUTONOMY RULES
- EXPLORE: Q&A codebase before modifying.
- PLAN: Brainstorm -> Plan -> Ask Approval -> Code.
- VERIFY: Self-run CLI/tests to validate outputs.
- TOOLS: Prioritize internal CLIs & MCP servers.
- GIT: Execute "commit, push, PR" strictly when instructed.
- MEMORY: Use '#' to persist explicit user rules here.
- CI/CD LOOP: Authorized for autonomous Build-Deploy-Test-Fix cycles during Lab development.
