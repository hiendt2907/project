# CLAUDE.md

**Omni** — async-first multi-agent SRE automation for K8s. Ollama diagnoses via 3 evidence lanes (state, app_log, metrics); split Kafka pipeline executes remediation.

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
        → Ollama LLM (qwen2.5:7b, num_ctx=4096)
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
| `analyst` | kafka_evidence_loop, kafka_action_feedback_loop |
| `core` | deep_scout, forecast, baseline_snapshot, proactive |
| `executor` | kafka_actions_loop |
| `full` | all (legacy monolith) |
| `siem-bridge` | Redis XREADGROUP → kafka omni-alerts |
| `evidence-adapter` | Redis XREADGROUP → kafka omni-diagnostic-evidence |
| `hitl-dispatcher` | omni-hitl-pending → FinGuard HITL API |
| `gateway` | FastAPI HTTP → kafka omni-alerts (separate image) |

---

## INVARIANTS

- Async-only: `asyncio`, `kubernetes-asyncio`, `redis[hiredis]`, `aiokafka`. No subprocess for K8s.
- `trace_id` end-to-end via `request_trace.py` push/pop.
- `OMNI_ENV_MODE=lab|prod` — enforced by `validate_env_mode_gate.py`.
- Ollama `num_ctx=4096` — DO NOT OVERRIDE.
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
- Event types: `ADVISORY_DECISION`, `ADVISORY_DISPATCHED`, `MUTATION_TRAPPED`.

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
- **LLM**: Ollama `VLLMClient` — `qwen2.5:7b` + `nomic-embed-text:latest` (768-dim)
- **RAG**: Redis Stack HNSW `redis_vector_store.py` + `semantic_cache.py`
- **Kafka**: `aiokafka`; `KafkaBus.send_dict(topic, dict)`
- **Tests**: pytest `asyncio_mode=auto` `pythonpath=src`; `FakeAsyncRedis(decode_responses=True)`; `_KafkaCapture.send_dict(topic, envelope)`. Context: `SimpleNamespace(redis, kafka, settings)`.

## Key Dirs

`src/workers/` · `src/gateway/` · `src/pkg/reasoning/` · `src/pkg/executor/` · `src/rag/` · `src/prober/` · `src/services/{analyst,playbook,evidence_adapter,audit_ledger}/` · `src/llm/` · `src/messaging/` · `k8s/deployments/` · `smart-siem/` (Go HITL API) · `tests/` · `tests/integration/`.

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

## Env

`OMNI_WORKER_ROLE` (prober|analyst|core|executor|full) · `OMNI_ENV_MODE` (lab|prod) · `OMNI_KAFKA_BOOTSTRAP_SERVERS` · `OMNI_REDIS_URL` · `OMNI_OLLAMA_BASE_URL` · `OMNI_AUDIT_PRIVATE_KEY_PATH`. Postgres removed — RAG on Redis Stack HNSW + semantic cache.

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
