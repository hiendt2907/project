# Architecture overview

## Split topology (Master Plan V3)

One Docker image (`multi-agent-system:latest`), multiple Deployments distinguished by `OMNI_WORKER_ROLE`:

| Role | Primary Kafka read → write |
|------|----------------------------|
| `prober` | `omni-alerts` → `omni-diagnostic-evidence` |
| `analyst` | `omni-diagnostic-evidence` + `omni-action-feedback` → `omni-actions` |
| `core` | Deep scout, proactive / forecast loops (optional Kafka proactive topics) |
| `executor` | `omni-actions` → `omni-action-feedback` |

Gateway is a **separate image** (`omni-gateway:latest`): HTTP only → Kafka `omni-alerts`. It must not import worker or reasoning code.

## End-to-end flow

1. **Ingest:** Prometheus/Alertmanager → Gateway → `omni-alerts` (envelope wraps JSON with `trace_id`).
2. **Diagnose:** Prober consumes alerts, runs K8s/Prom/Loki probes, publishes diagnostic evidence batches.
3. **Reason:** Analyst consumes evidence, applies RAG/gates/LLM, emits `SUGGEST_REMEDIATION` or `EXECUTE_MUTATE` to `omni-actions`.
4. **Execute:** Executor applies allowlisted K8s mutations, publishes feedback to `omni-action-feedback`.
5. **Learn:** Analyst consumes feedback; optional post-mutate SDK verify and RAG upsert.

## Trace ID

Every flow keeps a single `trace_id` from gateway through probes, reasoning, and execution. If absent at ingress, the gateway generates one (`gw-prom-…`).

## Related code

- Worker entry: `src/workers/omni_worker.py`
- Settings: `src/workers/settings.py`
- Canonical doc: `docs/vendor/OMNI_PROJECT_CANONICAL.md`
