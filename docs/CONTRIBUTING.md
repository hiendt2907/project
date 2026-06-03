# Contributing to Omni SRE

<!-- AUTO-GENERATED sections marked below. Hand-written prose above the markers is preserved. -->

## Prerequisites

- Python 3.11+
- Docker + OrbStack (K8s runtime for local cluster)
- Ollama running on macOS host with `qwen3.6` and `nomic-embed-text` models
- `kubectl` configured for OrbStack

## Development Setup

```bash
# Clone and install
git clone <repo> && cd project
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Copy and fill environment
cp .env.example .env
# Edit .env: fill TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# Build images
make docker-worker docker-gateway

# Deploy full stack
make deploy-kafka deploy-ollama && make ensure-kafka-topics
make deploy-worker deploy-gateway deploy-siem-stack

# Ingest RAG training data
kubectl port-forward -n multi-agent svc/redis 16379:6379 &
PYTHONPATH=src .venv/bin/python src/training/advisory_ingest.py \
  --path data/rag_training/omni_sop_samples.jsonl \
  --redis-url redis://localhost:16379/0
```

## Testing

```bash
# Unit tests (no real infra needed)
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration

# Integration tests (requires live K8s + Redis + Kafka)
.venv/bin/python -m pytest tests/integration/ -q

# Coverage gate (90% threshold)
make coverage-gate

# Full autonomy regression suite
make autonomy-gate

# Advisory quality benchmark (informational, needs Ollama)
OMNI_OLLAMA_BASE_URL=http://localhost:11434 make benchmark-advisory
```

### Test Rules
- `asyncio_mode=auto` — all tests are async-capable without `@pytest.mark.asyncio`
- Use `FakeAsyncRedis(decode_responses=True)` not `AsyncMock()` for Redis tests
- Use `SimpleNamespace(redis, kafka, settings)` as context in unit tests
- Integration tests live in `tests/integration/` and require `-m integration` to run

## <!-- AUTO-GENERATED: COMMANDS --> Available Commands

| Command | Description |
|---------|-------------|
| `make docker-worker` | Build omni-worker Docker image |
| `make docker-gateway` | Build omni-gateway Docker image |
| `make deploy-worker` | Apply K8s worker deployment manifests |
| `make deploy-gateway` | Apply K8s gateway deployment manifests |
| `make deploy-kafka` | Deploy Kafka (KRaft, single-broker) |
| `make deploy-ollama` | Apply Ollama ExternalName service |
| `make deploy-siem-stack` | Deploy siem-bridge + evidence-adapter + hitl-dispatcher |
| `make ensure-kafka-topics` | Create all required Kafka topics |
| `make rag-hot-sync` | Ingest SOPs + K8s docs + HITL history into HNSW |
| `make coverage` | Run test coverage report |
| `make coverage-gate` | Business scope coverage (≥90% threshold) |
| `make autonomy-gate` | Autonomy regression tests gate |
| `make secret-gate` | Gitleaks secret scan (CI gate) |
| `make benchmark-advisory` | Advisory quality benchmark (100pt rubric) |
| `make chaos-drill` | Chaos drill on all 4 lanes |
| `make chaos-drill-rollback` | Inject bad ConfigMap → verify auto-rollback + CRAT |
| `make omni-death-loop` | Continuous fault injection loop (NS=multi-agent required) |
| `make e2e-proactive` | Run proactive E2E test |
| `make e2e-incident-matrix` | Run incident matrix E2E test |
| `make rollback` | kubectl rollout undo worker + gateway |
| `make asyncio-lint` | Check for blocking asyncio calls |
| `make sbom` | Generate Software Bill of Materials |

<!-- END AUTO-GENERATED: COMMANDS -->

## Code Style

- Async-only: `asyncio`, `kubernetes-asyncio`, `redis[hiredis]`, `aiokafka`. No subprocess for K8s.
- Mutations only via executor; analyst is read-only.
- `src/gateway/` must NOT import worker/executor/prober modules.
- `OMNI_AUTO_EXECUTE_ENABLED=false` default — never bypass the kill-switch in tests.
- New evidence source types must have an early-return branch after `coerce_evidence_dict()`.

## PR Checklist

- [ ] `make autonomy-gate` passes (`pass: true`)
- [ ] `make coverage-gate` ≥ 90%
- [ ] `make secret-gate` clean (no secrets leaked)
- [ ] `make asyncio-lint` clean (no blocking calls in async loops)
- [ ] New Redis ZSET code uses `FakeAsyncRedis` (not `AsyncMock`) in tests
- [ ] No cluster-admin RBAC added to executor
- [ ] CRAT write precedes any Telegram emit or action dispatch

## <!-- AUTO-GENERATED: ENV --> Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Yes | Telegram chat ID for advisory notifications |
| `LLM_BASE_URL` | Yes | Ollama OpenAI-compatible base URL |
| `LLM_MODEL_NAME` | Yes | LLM model (default: qwen2.5-coder:7b) |
| `EMBED_BASE_URL` | Yes | Embedding model base URL |
| `EMBED_MODEL_NAME` | Yes | Embedding model (default: nomic-embed-text) |
| `OMNI_WORKER_ROLE` | Yes | prober\|analyst\|core\|executor\|full |
| `OMNI_ENV_MODE` | Yes | lab\|prod |
| `OMNI_KAFKA_BOOTSTRAP_SERVERS` | Yes | Kafka bootstrap address |
| `OMNI_REDIS_URL` | Yes | Redis URL |
| `OMNI_OLLAMA_BASE_URL` | Yes | Ollama base URL |
| `OMNI_AUTO_EXECUTE_ENABLED` | No | Master kill-switch (default: false = suggest only) |
| `OMNI_LLM_NUM_CTX` | No | Context window tokens (default: 8192) |
| `OMNI_AUDIT_PRIVATE_KEY_PATH` | No | Ed25519 PEM key for CRAT signing (unset = lab/unsigned) |
| `OMNI_GATEWAY_API_KEY` | No | Master API key for gateway auth |
| `OMNI_TENANT_APIKEYS` | No | Per-tenant keys: `tenant_id:key,tenant_id2:key2` |

<!-- END AUTO-GENERATED: ENV -->
