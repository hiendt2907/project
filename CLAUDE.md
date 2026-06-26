# CLAUDE.md

> **TRƯỚC MỌI TASK: đọc `MEMORY.md` + `docs/CODEBASE.md`.** Bản đồ nhanh ở memory `project_architecture_map`; chi tiết file-level ở `docs/CODEBASE.md`.

**Omni** — async-first multi-agent SRE automation for K8s. Ollama diagnoses via 4 evidence lanes; split Kafka pipeline executes remediation.

## DIAGNOSTIC FLOWS

| Lane | Signal | Key file |
|---|---|---|
| 1 SYS_RESOURCE | 3σ z-score CPU/mem | `anomaly/three_sigma.py`, `baseline_snapshot.py` |
| 2 SYS_HARD_FAIL | OS state machine + LLM | `os_state_validator.py`, `AnalystAdvisory` schema |
| 3 APP_HTTP | HTTP status classes (5xx/429/401) | `log_surge_probe.py` |
| 4 SIEM_SECURITY | FinGuard incidents, kill-chain | `siem_reasoning.py`, `_siem_diagnosis_from_batch()` |

**Advisory schema** (`src/pkg/reasoning/analyst_advisory_schema.py`): WHAT/WHO/WHY/HOW-TO + ForecastTimeline (5 horizons). L1→L4: os_baremetal → network → kubernetes → prometheus.
**Telegram**: `unified_incident_card.py` — nhãn VI (Sự cố/Workload/Kiểm chứng/Khắc phục/Dự báo/🧾 Audit). WHAT/WHO/WHY/HOW-TO = marker máy, KHÔNG đổi (parse-coupled).

## KNOWLEDGE PIPELINE (2026-06-27, commit c4635ab)

`INV_KNOWLEDGE_NOT_ALERT`: non-ANOMALY signals KHÔNG vào `omni-diagnostic-evidence`.
- `signal_type` trong `build_envelope()`: ANOMALY → `omni-diagnostic-evidence`; METRIC_SAMPLE/LOG_SAMPLE/DISCOVERY → `omni-knowledge-evidence`
- `src/workers/knowledge_pipeline.py`: dispatcher không RAG/LLM; rolling log LPUSH+LTRIM 500/24h; change detection diff → Telegram approve/reject
- `src/anomaly/remote_host_baseline.py`: `ConfidenceLevel` (STATIC_GUARD 0-24 / LEARNING 25-49 / ASSISTED 50-74 / AUTONOMOUS 75-100); `add_confidence(delta)`, `decay_confidence(-5/day)`; key `omni:3sigma:confidence:{tenant}:{host}` TTL=30d
- `src/remote_agent/discovery.py`: `save/load_discovery_snapshot()`, `diff_discovery()` (SERVICE_ADDED/REMOVED, PORT_OPENED/CLOSED); agent re-discovery mỗi 1h
- `src/services/knowledge/document_store.py`: `ingest_customer_knowledge()` — metadata only (INV_DATA_RESIDENCY); +20 confidence per doc
- Kafka topic: `omni-knowledge-evidence` (partitions=3, retention=7d); env `OMNI_KAFKA_TOPIC_KNOWLEDGE_EVIDENCE`

## PIPELINE

Remote agents → `omni-knowledge-evidence` → knowledge_pipeline (no RAG/LLM)
Alert sources → `omni-diagnostic-evidence` → analyst (RAG → LLM → AnalystAdvisory → CRAT [FAIL-CLOSED] → SUGGEST/EXECUTE/HITL)
`omni-actions` → executor → `omni-action-feedback` → re-evaluation

## COMPONENT ROLES (OMNI_WORKER_ROLE)

| Role | Active loops |
|---|---|
| `full` | tất cả: evidence, actions, feedback, kpi, knowledge, siem-chains, tier |
| `analyst` | kafka_evidence_loop, action_feedback, kpi, knowledge, siem-chains, tier |
| `prober` | kafka_alerts_loop, delayed_queue, circuit_breaker, telegram_polling |
| `core` | deep_scout, forecast, baseline_snapshot, proactive |
| `executor` | kafka_actions_loop |
| `gateway` | FastAPI HTTP → kafka omni-alerts (separate image) |

## INVARIANTS (vi phạm = bug)

- Async-only: `asyncio`, `kubernetes-asyncio`, `redis[hiredis]`, `aiokafka`. No subprocess for K8s.
- `src/gateway/` KHÔNG import `workers/`. Shared code → `src/pkg/`.
- Mutations only via executor; analyst is read-only.
- `OMNI_AUTO_EXECUTE_ENABLED=false` — master kill-switch (fail-closed).
- **CRAT Fail-Closed**: `write_audit_block()` MUST succeed trước Telegram emit / action dispatch.
- `kafka_evidence_loop` dùng `auto_offset_reset="earliest"` — KHÔNG đổi thành `latest`.
- `omni-audit-chain` topic cần message key (compact policy).
- `INV_NO_RESTART_ON_BROKEN_SPEC` · `INV_READ_BEFORE_MUTATE` · `INV_NAMESPACE_ISOLATION` · `ERR_REA_NO_PHYSICAL_PROOF` · `ERR_GOV_UNAUTHORIZED_MUTATION`
- `INV_KNOWLEDGE_NOT_ALERT`: non-ANOMALY signals KHÔNG vào omni-diagnostic-evidence.
- `INV_DATA_RESIDENCY`: tài liệu khách hàng chỉ lưu metadata trên Omni (file_id + summary ≤2000 chars).
- RBAC: `omni-worker` SA không có Secrets. Executor: NEVER cluster-admin.
- `OMNI_LLM_NUM_CTX` default 8192. Dùng `build_llm_options(ctx)` — không inline getattr.
- Autonomy tier: `resolve_tier` ưu tiên Redis cache `omni:cfg:tier:{tenant}` > PG > env. Đổi env phải DEL cache.

## CRAT (SOX §404, PCI-DSS v4.0)

`src/services/audit_ledger/` — SHA-256 hash-chain + Ed25519. Events: `ADVISORY_DECISION`, `ADVISORY_DISPATCHED`, `MUTATION_TRAPPED`, `HITL_DECISION`, `ROLLBACK_EXECUTED`.
`OMNI_AUDIT_PRIVATE_KEY_PATH` — PEM Ed25519 (unset = unsigned, lab only).

## INFRASTRUCTURE

- **K8s**: OrbStack, namespace `multi-agent`; pod duy nhất `omni-fullstack` (role=full). `make deploy-worker` = `deploy-fullstack`.
- **LLM**: Ollama `qwen2.5-coder:7b` (active) + `nomic-embed-text:latest` (768-dim). Host: `host.orb.internal:11434`.
- **DB**: PostgreSQL `omni_admin` schema = source-of-truth autonomy config. Redis = hot-path cache + RAG HNSW + audit chain.
- **Tests**: pytest `asyncio_mode=auto` `pythonpath=src`; dùng `FakeRedis(decode_responses=True)` cho ZSET tests (không AsyncMock).

## KEY DIRS

`src/workers/` · `src/gateway/` · `src/remote_agent/` · `src/anomaly/` · `src/services/{analyst,audit_ledger,knowledge}/` · `src/rag/` · `src/pkg/` · `k8s/deployments/` · `tests/`

## COMMANDS

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration   # unit tests
make deploy-worker deploy-gateway ensure-kafka-topics              # deploy
make e2e-proactive e2e-incident-matrix                             # E2E
curl localhost:8090/healthz && curl localhost:8090/readyz          # health
make benchmark-advisory                                            # advisory quality
NS=multi-agent make omni-death-loop                                # chaos loop
```

## ENV (critical)

`OMNI_WORKER_ROLE` · `OMNI_ENV_MODE` (lab|prod) · `OMNI_KAFKA_BOOTSTRAP_SERVERS` · `OMNI_REDIS_URL` · `OMNI_OLLAMA_BASE_URL` · `OMNI_AUTO_EXECUTE_ENABLED` (default false) · `OMNI_LLM_NUM_CTX` (default 8192) · `OMNI_KAFKA_TOPIC_KNOWLEDGE_EVIDENCE` (default omni-knowledge-evidence) · `OMNI_AUDIT_PRIVATE_KEY_PATH` · `OMNI_TENANT_APIKEYS` (tenant_id:key,...) · `OMNI_GATEWAY_API_KEY`

## DEPLOYMENT STATE (2026-06-27)

Pod `omni-fullstack` 1/1 Running, role=full, tier=shadow (observe-only). Kill-switch ON. RAG `omni:rag:sop` HLEN≥1010. Redis AOF enabled. Knowledge pipeline active (omni-knowledge-evidence, 3 partitions).

## COMMUNICATION

- **Code first.** Viết code ngay, không hỏi lại.
- **Giải thích tối đa 100 chữ** khi thật sự cần.

## AUTONOMY RULES

EXPLORE → PLAN → VERIFY → GIT (chỉ khi được chỉ thị). CI/CD loop tự động trong Lab. `#` để ghi rule mới vào đây.
