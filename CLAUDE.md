# CLAUDE.md

**Omni** — async-first multi-agent SRE automation for K8s. Ollama diagnoses via 3 evidence lanes (state, app_log, metrics); split Kafka pipeline executes remediation.

Refs: `docs/vendor/OMNI_PROJECT_CANONICAL.md` · `docs/DOCUMENTATION_INDEX.md` · `docs/reports/project-memory.md` · `docs/vendor/knownbase.md`

## Topology (one image, role via `OMNI_WORKER_ROLE`)

| Component | Kafka flow |
|---|---|
| Prober | `omni-alerts` → `omni-diagnostic-evidence` |
| Analyst | evidence + `omni-action-feedback` → `omni-actions` |
| Core | periodic scout, anomaly, forecast |
| Executor | `omni-actions` → `omni-action-feedback` |
| Gateway (separate image) | HTTP → `omni-alerts` |

**SIEM**: `omni-siem-bridge`, `omni-evidence-adapter`, `omni-hitl-dispatcher` (FinGuard HITL for critical mutations).

## Invariants

- Async-only: `asyncio`, `kubernetes-asyncio`, `redis[hiredis]`, `aiokafka`. No subprocess for K8s.
- `trace_id` end-to-end.
- `OMNI_ENV_MODE=lab|prod` — enforced by `validate_env_mode_gate.py`.
- Ollama `num_ctx=4096` — do not override.
- `src/gateway/` must NOT import worker/executor/prober.
- Mutations only via executor; analyst is read-only.
- Containers: `USER appuser` (uid 10001).
- Secrets: env + K8s Secrets only; gitleaks CI gate.
- Executor RBAC: NEVER cluster-admin.

## Key Dirs

`src/workers/` · `src/gateway/` · `src/pkg/reasoning/` · `src/pkg/executor/` · `src/rag/` (Redis vector + semantic cache) · `src/prober/` · `src/services/{analyst,playbook,evidence_adapter}/` · `src/llm/` (Ollama) · `src/messaging/` · `src/observability/` · `tests/`, `tests/integration/`.

## Commands

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
.venv/bin/python -m pytest tests/integration/ -q
make autonomy-gate
make docker-worker docker-gateway
make deploy-worker deploy-gateway deploy-kafka deploy-ollama
make ensure-kafka-topics deploy-siem-stack
make e2e-proactive e2e-incident-matrix lab-nginx-cpu rag-hot-sync
```

CI order: build → rollout → unit → E2E.

## Stack Standards

- **Python**: async-first, Pydantic settings, pytest `asyncio_mode=auto`, `pythonpath=src`. Mock: `FakeAsyncRedis(decode_responses=True)`; Kafka via `_KafkaCapture.send_dict(topic, envelope)`. Context: `SimpleNamespace(redis, kafka, settings)`.
- **Next.js/TS**: strict types; server components default; URL as shareable state.
- **Redis Stack**: `src/rag/redis_vector_store.py` (replaces pgvector), `semantic_cache.py`. HNSW indexes + TTL.
- **K8s (OrbStack)**: manifests `k8s/deployments/`. `lab` vs `prod` ConfigMaps. Least-privilege RBAC.

## Env

`OMNI_WORKER_ROLE` (prober|analyst|core|executor|full) · `OMNI_ENV_MODE` (lab|prod) · `OMNI_KAFKA_BOOTSTRAP_SERVERS` · `OMNI_REDIS_URL` · `OMNI_OLLAMA_BASE_URL`. Postgres removed — RAG on Redis Stack HNSW + semantic cache.
