# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Omni** is an async-first, multi-agent SRE automation system for Kubernetes. It uses LLMs (Ollama) to diagnose infrastructure incidents through a three-lane evidence model (resource state, application logs, metrics) and executes autonomous remediation via a split-topology Kafka pipeline.

Canonical architecture reference: `docs/vendor/OMNI_PROJECT_CANONICAL.md`
Full doc index: `docs/DOCUMENTATION_INDEX.md`
Symptom → fix knowledge base: `docs/vendor/knownbase.md`
Invariants and guardrails: `docs/reports/project-memory.md`

## OMNI Autonomous Platform

### Core Philosophy: "Learn from past, live at present, plan for future"

- **Learn from past:** Analyze historical logs, incidents, and post-mortems (Vector DB/Knowledge Base).
- **Live at present:** Real-time K8s auto-operations, strict RBAC, least-privilege execution, and self-healing.
- **Plan for future:** Predictive scaling, capacity planning, and proactive security hardening.

### Evolution Roadmap

- **Phase 1 (Current):** Kubernetes autonomous operations.
- **Phase 2+:** Expand to System (OS), Databases (PostgreSQL, MySQL), APIs, and source code.

### Architectural Rules

- **Doc-Driven:** Always base designs on official documentation (K8s, DB, Security).
- **Strict Isolation:** Differentiate Lab vs. Prod environments via ConfigMaps.
- **Zero-Trust:** Omni-executor MUST NEVER run as `cluster-admin`. All mutations require validation gates.

## Commands

### Testing
```bash
# All unit tests
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration

# Single test function
.venv/bin/python -m pytest tests/test_autonomous_contract.py::test_build_execute_mutate_body -v

# Single test file
.venv/bin/python -m pytest tests/test_autonomous_contract.py -q

# Integration tests only
.venv/bin/python -m pytest tests/integration/ -q

# Stateful ReAct E2E (sim cluster + glassbox audit trail)
.venv/bin/python -m pytest tests/integration/test_e2e_autonomous_loop.py -v
# OMNI_E2E_AUDIT_JSON=/tmp/omni_e2e_audit.json .venv/bin/python -m pytest tests/integration/test_e2e_autonomous_loop.py -v

# Tests matching a keyword
.venv/bin/python -m pytest tests/ -k "autonomous" -q

# Contract tests (CI mandatory)
pytest -q --tb=short \
  tests/test_autonomous_contract.py \
  tests/test_analyst_agentic_loop.py \
  tests/test_diagnostic_mapping.py \
  tests/test_evidence_proof_gate.py \
  tests/test_proactive_guardrails.py \
  tests/integration/test_autonomy_loop_transitions.py \
  tests/integration/test_autonomy_transition_contract_strict.py
```

### Validation Gates
```bash
python scripts/validate_env_mode_gate.py
python scripts/validate_mutate_only_gate.py
python scripts/validate_classifier_regression_gate.py
python scripts/validate_phase_docs_gate.py
python scripts/validate_nonimpact_guards_gate.py
python scripts/validate_learning_loop_gate.py
make autonomy-gate   # full gate suite
```

### Build & Deploy
```bash
make docker-worker     # builds multi-agent-system:latest
make docker-gateway    # builds omni-gateway:latest
make deploy-worker     # deploys prober, analyst, core, executor to K8s
make deploy-gateway
make deploy-kafka
make deploy-ollama
make ensure-kafka-topics
```

### E2E / Lab
```bash
make e2e-proactive
make e2e-incident-matrix
bash scripts/gateway_alert_loki_verify.sh
make lab-nginx-cpu
make rag-hot-sync
```

### CI Pipeline (run after changes to worker/gateway/Dockerfile/requirements)
Build image → Deploy + rollout → Unit tests → E2E. See `.cursor/rules/omni-cicd-k8s.mdc`.

## Architecture

### Split Topology (Master Plan V3)

Five processes, each a separate K8s Deployment and Docker image:

| Component | `OMNI_WORKER_ROLE` | Kafka: reads from → writes to |
|---|---|---|
| **Omni-Prober** | `prober` | `omni-alerts` → `omni-diagnostic-evidence` |
| **Omni-Analyst** | `analyst` | `omni-diagnostic-evidence` + `omni-action-feedback` → `omni-actions` |
| **Omni-Core** | `core` | Periodic deep scout, anomaly detection, forecasting |
| **Omni-Executor** | `executor` | `omni-actions` → `omni-action-feedback` |
| **Omni-Gateway** | _(separate image)_ | HTTP ingress → `omni-alerts` |

**Full incident flow:**
1. Prometheus alertmanager → Gateway (HTTP, rate-limited) → `omni-alerts`
2. Prober diagnoses via 3 evidence lanes (K8s resource state, application logs, Prometheus metrics) → `omni-diagnostic-evidence`
3. Analyst runs LLM ReAct loop, classifies incident, generates action → `omni-actions`
4. Executor applies K8s mutation → `omni-action-feedback`
5. Analyst reads feedback, updates pgvector RAG collections (experience replay)

### Key Source Directories

| Path | Purpose |
|---|---|
| `src/workers/` | Worker entrypoint, Kafka consumer loops, settings (Pydantic), handler context |
| `src/gateway/` | FastAPI ingress — zero imports from worker/executor modules |
| `src/pkg/reasoning/` | LLM reasoning, diagnostic policies, tool selection |
| `src/pkg/executor/` | K8s mutation actions, sandbox execution |
| `src/rag/` | pgvector store, semantic search, collection management |
| `src/prober/` | Diagnostic probes (K8s, Prometheus, Loki, network) |
| `src/services/analyst/` | Analyst boundary (reasoning only, no direct mutations) |
| `src/llm/` | Ollama client, LLM routing, token management |
| `src/messaging/` | Kafka bus, Redis streams, async message handling |
| `src/observability/` | OpenTelemetry tracing, structured logging, Prometheus metrics |
| `src/training/` | Learning loops, experience replay |
| `src/anomaly/` | Time-series anomaly detection and forecasting |
| `tests/` | pytest suite (480+ tests); `tests/integration/` for async loop tests |

### Core Invariants

- **Async-first**: all code uses `asyncio`. K8s via `kubernetes-asyncio` (no subprocess). Redis via `redis[hiredis]`. Kafka via `aiokafka`.
- **Trace ID**: every business flow carries a `trace_id` from ingest through execution and reporting.
- **`OMNI_ENV_MODE`**: `lab` = full authority; `prod` = least privilege (guards enforced by `validate_env_mode_gate.py`).
- **Context window**: Ollama clients use `num_ctx=4096` — do not override this without coordinating the full pipeline.
- **Gateway isolation**: `src/gateway/` must never import from worker, executor, or prober modules.
- **Mutate-only guard**: mutations may only execute through the executor role; analyst is read-only.
- **Non-root containers**: all Dockerfiles run as `USER appuser` (uid 10001).

### Environment Variables (key subset)

Defined in `src/workers/settings.py` via Pydantic settings:

- `OMNI_WORKER_ROLE` — `prober|analyst|core|executor|full`
- `OMNI_ENV_MODE` — `lab|prod`
- `OMNI_KAFKA_BOOTSTRAP_SERVERS`, `OMNI_REDIS_URL`, `OMNI_OLLAMA_BASE_URL`, `OMNI_POSTGRES_RAG_DSN`

### Testing Patterns

- `pytest.ini`: `asyncio_mode = auto`, `pythonpath = src`, `testpaths = tests`
- Redis mocked with `FakeAsyncRedis(decode_responses=True)` from `fakeredis`
- Kafka mocked with custom `_KafkaCapture` class (`send_dict(topic, envelope)`)
- Context objects built with `SimpleNamespace(redis=..., kafka=..., settings=...)`
- Contract tests in CI enforce autonomy loop transitions and evidence gate invariants

### Secrets & Security

- Secrets managed via K8s Secrets and environment variables only; enforced by gitleaks in CI (`.gitleaks.toml`)
- Secret scan runs as a critical-fail step in `.github/workflows/ci.yml`
