# Unified refactor roadmap

Single source of truth for the multi-phase refactor. **Newest plan iterations** may live under `.cursor/plans/`; canonical source plans referenced:

- Redis standalone: `redis_standalone_migration_*.plan.md` (Cursor plans dir)
- SRE Diagnostic Dictionary: `sre_diagnostic_dictionary_*.plan.md`
- Knowledge PGVector: `knowledge-worker_pgvector_*.plan.md`

## Git conventions

| Item | Value |
|------|--------|
| Remote | `git@github.com:hiendt2907/project.git` |
| Commit prefix | `[x]` on completed milestones (subject or body) for filtering |
| Working branch | `main` (or your feature branch) |

## Legacy vs canonical K8s

- **Canonical lab manifests:** `k8s/deployments/`
- **Legacy / alternate:** `deployments/` — do not bulk-edit unless your pipeline uses them; note only.

---

## Phase 0 — Roadmap and conventions

| Item | Status |
|------|--------|
| This file + Git table | Done |
| Link three source plans (above) | Done |
| Do not mix `deployments/` with `k8s/deployments/` without intent | Done |

---

## Phase 1 — Redis standalone + AOF

| Item | Status |
|------|--------|
| `k8s/deployments/redis-standalone.yaml` (Service `redis`, STS/PVC, AOF) | Done |
| `omni-worker-configmap`: `OMNI_REDIS_CLUSTER=false`, remove cluster nodes | Done |
| Retire `redis-cluster` SS/job/PVC in lab | Done (manual: delete old manifests / PVC) |
| `scripts/deploy_v6.sh` | Done |
| `k8s/monitor/redis-exporter.yaml`, `prometheus.yaml`, `grafana-alerting-provisioning.yaml` | Done |
| `scripts/full_system_audit.py`, chaos script/tests | Done |
| Verify + `[x]` commit + `knownbase.md` if needed | Done |

---

## Phase 2 — PGVector vendor partition

| Item | Status |
|------|--------|
| `COLLECTION_VENDOR_KNOWLEDGE` + `doc_vendor` + HNSW in `pgvector_store` + `deployments/schema.sql` | Done |

---

## Phase 3 — `src/knowledge/` (Clean + Chunk mandatory)

| Item | Status |
|------|--------|
| Package + pipeline gate (no raw HTML to embed) | Done |
| Tests HTML → clean → chunk | Done |
| ConfigMap + CronJob | Done |

---

## Phase 4 — Retrieval tool

| Item | Status |
|------|--------|
| `query_points` + `payload_filters` | Done |
| `tool_vendor_knowledge_search` + prompts + settings | Done |

---

## Phase 5 — SRE Diagnostic Dictionary

| Item | Status |
|------|--------|
| `config/diagnostic_matrix.yaml` (seed from `docs/vendor/knownbase.md`) | Done |
| registry + dispatcher + evidence + proactive `XADD` | Done |
| Tests | Done |

---

## Phase 6 — Cleanup

| Item | Status |
|------|--------|
| Remove Redis Cluster code paths | Done |
| Mark all phases Done in this file | Done |
