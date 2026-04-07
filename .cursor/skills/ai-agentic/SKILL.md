---
name: ai-agentic
description: Guides autonomous and agentic flows in the Omni lab repo—omni-actions (SUGGEST_REMEDIATION vs EXECUTE_MUTATE), analyst RAG/agentic mutate, executor allowlist, feedback Kafka, and CI verify. Use when the user mentions agentic, autonomous remediation, omni-actions, rollout restart, proactive ReAct, EXECUTE_MUTATE, or when changing workers/gateway for self-healing behavior.
---

# AI Agentic (Omni lab)

## Scope

This repository splits **suggestion** (audit) from **mutation** (execute). Agentic code lives mainly under `src/workers/`, `src/pkg/rag/`, and `pkg/autonomous_actions`. Prefer **Kafka** for action queues; Redis for locks, CB, session/trace, delayed ZSET—not list queues.

## Core concepts

| Concept | Role |
|---------|------|
| **SUGGEST_REMEDIATION** | Kafka `omni-actions` payload: diagnosis text + metadata; **executor audit-only** (no kubectl mutate). |
| **EXECUTE_MUTATE** | Same topic; executor runs **allowlisted** tools (e.g. `k8s_rollout_restart`) when `OMNI_AUTO_EXECUTE_ENABLED=true`. |
| **omni-action-feedback** | Executor publishes result; analyst may consume for replan/cache (see `workers/autonomous_feedback_loop.py`). |
| **RagGate** | `evaluate_rag_gate` in `src/pkg/rag/gate.py`—embed (Ollama) then pgvector; failures log `phase=ollama_embed\|pgvector_query`. |
| **CPU incident rollout** | `omni_autonomous_rollout_on_cpu_incident` + `_emit_cpu_rollout_if_eligible` in `evidence_consumer.py`—emit mutate when deployment+CPU alert even if RAG suggests read-only tools. |
| **trace_id** | Must propagate across gateway → prober → analyst → executor; log grep uses same id. |

## Files to open first (typical tasks)

- Evidence path: [`src/workers/evidence_consumer.py`](src/workers/evidence_consumer.py), [`src/workers/analyst_agentic_loop.py`](src/workers/analyst_agentic_loop.py)
- Emit/contract: [`src/workers/evidence_mutate_emit.py`](src/workers/evidence_mutate_emit.py), [`pkg/autonomous_actions.py`](pkg/autonomous_actions.py)
- Executor: [`src/workers/kafka_actions_consumer.py`](src/workers/kafka_actions_consumer.py), [`src/workers/autonomous_execute.py`](src/workers/autonomous_execute.py)
- Settings: [`src/workers/settings.py`](src/workers/settings.py) (`omni_auto_execute_enabled`, `omni_autonomous_rollout_on_cpu_incident`, Kafka topic names)
- Ops/debug: [`docs/vendor/knownbase.md`](docs/vendor/knownbase.md) (RAG DNS, Ollama ExternalName, Postgres DSN)

## Implementation checklist

When adding or changing agentic behavior:

1. **Contract**: `EXECUTE_MUTATE` chỉ **Kubernetes SDK** (`K8S_SDK_EXECUTE_TOOL_NAMES` trong `autonomous_execute.py`): `k8s_*`, `inspect_*`, `list_*`, `resolve_*`, `kubectl_cluster` — không echo/shell/metrics. Thêm tool K8s mới → bổ sung tên vào constant đó + verify test `test_mutate_allowlist_k8s_sdk_only`.
2. **Safety**: Respect `autonomous_allowed_namespaces` and pre-apply revalidate for rollout (`deployment_evidence_snapshot`).
3. **Tests**: Extend `tests/test_autonomous_contract.py` or targeted handler tests; run `pytest` on touched modules.
4. **CI/CD** (worker/gateway/runtime): Per `.cursor/rules/omni-cicd-k8s.mdc`—`docker build`, `make deploy-worker`, at least one e2e script when cluster available.
5. **Docs**: If a new failure mode is fixed, append a short **Symptom / Fix** to `docs/vendor/knownbase.md` (no duplicate symptoms).

## Anti-patterns

- Emitting mutate without a clear gate (auto-execute off in prod unless explicitly enabled).
- Skipping `trace_id` on new Kafka payloads.
- Using Redis `BLPOP` for worker job queues (use Kafka consumer groups).

## Quick verify (lab)

```bash
# Ollama reachable from analyst (RAG embed)
kubectl exec deploy/omni-analyst -n multi-agent -- curl -sS -o /dev/null -w "%{http_code}\n" http://ollama-service:11434/api/tags
```

Expected `200` when Ollama-on-host + `make deploy-ollama` is applied.

## Additional resources

- Split topology and e2e scripts: `.cursor/rules/omni-cicd-k8s.mdc`, `scripts/gateway_alert_loki_verify.sh`, `scripts/nginx_test_cpu_alert_lab.sh`
