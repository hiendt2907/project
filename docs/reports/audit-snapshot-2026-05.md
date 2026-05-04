# Audit snapshot — 2026-05 (Omni + Smart-SIEM)

Periodic registry: **what the system is for**, **where truth lives**, **what was verified** this pass. Cursor sessions do not retain chat memory; agents should **read this file + links** at the start of large refactors.

## North Star (original system intent)

1. **Omni** ingests alerts/evidence, correlates with `trace_id`, runs read-heavy diagnosis (K8s SDK, Prometheus, optional Loki), and uses an LLM for structured advisory output — **not** silent auto-mutate in default mode.
2. **Split workers (MPV3):** `omni-prober` consumes `omni-alerts` and publishes `omni-diagnostic-evidence`; `omni-analyst` consumes evidence and may emit `omni-actions`; `omni-executor` is the only path that should execute mutations from that bus.
3. **CRAT is fail-closed:** no Telegram advisory dispatch and no action dispatch until `write_audit_block()` succeeds (Redis chain + `omni-audit-chain`).
4. **Glassbox:** one trace flows ingest → evidence → plan/suggestion → optional feedback → re-evaluation; do not drop LLM JSON without an explicit `TRUNCATED` or error reason.
5. **FinGuard Smart-SIEM** (under `smart-siem/`): customer SOC runtime in **Go/Rust** on-prem / air-gapped posture; single controlled egress story per `AGENTS.md` and bank docs — **no Python** in security-critical backend paths there.

## Authority map (read order)

| Layer | Path | Role |
|-------|------|------|
| Omni pipeline + invariants | [CLAUDE.md](../../CLAUDE.md) | End-to-end diagram, `OMNI_WORKER_ROLE`, CRAT, pytest/Make |
| Omni ops single source | [OMNI_PROJECT_CANONICAL.md](../vendor/OMNI_PROJECT_CANONICAL.md) | Split deploy, Kafka topic names, feedback topic |
| Smart-SIEM program | [smart-siem/AGENTS.md](../../smart-siem/AGENTS.md) | Planning hierarchy, layout, air-gap pointers |
| Locked deltas / incidents | [project-memory.md](project-memory.md) | Invariants and postmortems — **not** a duplicate of CLAUDE |

## Repository scope (this checkout)

- **Omni:** repo root — `src/`, `k8s/deployments/`, `scripts/`, `tests/`.
- **Smart-SIEM:** `smart-siem/` with Go modules under `smart-siem/omni/siem/{agent,bff,brain-go,math-gateway,license-validator}`, `smart-siem/customer/{ui,audit-pipeline/...}`, `smart-siem/provider/{backend,license-drm,playbook-forge}` (when present).

## Documentation gap (blocked, do not invent)

- [smart-siem/AGENTS.md](../../smart-siem/AGENTS.md) references **`docs/MASTER_PLAN_V15.md`** (and V14/V14.1) **relative to `smart-siem/`**. Those files are **not present in this workspace snapshot** (glob 2026-05). Treat program waves as **out-of-tree or not checked out** until paths exist; do not fabricate wave contents from memory.

## Verify matrix — last run (fill on each audit pass)

Commands below were executed as part of implementing this snapshot.

### Omni (Python)

| Step | Command | Result |
|------|---------|--------|
| Unit tests | `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` | **PASS** — 399 passed (2026-05-04) |
| Integration (optional) | `.venv/bin/python -m pytest tests/integration/ -q` | _(not run this pass)_ |
| E2E alert (cluster) | `SLEEP_SEC=15 STRICT_ASSERT=0 … bash scripts/gateway_alert_loki_verify.sh` (default HighCPU payload) | **PASS** — trace `gw-prom-3ee38bccd131` (2026-05-04, lab `multi-agent`) |

### Smart-SIEM (Go)

| Module | Command | Result |
|--------|---------|--------|
| `smart-siem/omni/siem/agent` | `go test ./...` | **PASS** (2026-05-04) |
| `smart-siem/omni/siem/bff` | `go test ./...` | **PASS** (2026-05-04) |
| `smart-siem/omni/siem/brain-go` | `go test ./...` | **PASS** (2026-05-04) |

_Update the Result column after each audit; keep one row per representative module or note "full monorepo skipped (time)"._

## Cursor guardrails (human + agent)

Copy into **Cursor Project Instructions** if desired:

> Before large changes: read `CLAUDE.md`, `docs/vendor/OMNI_PROJECT_CANONICAL.md`, and `smart-siem/AGENTS.md`. Do not change `kafka_evidence_loop` `auto_offset_reset` away from `earliest`. Mutations only via `omni-executor`. For Omni worker/gateway/runtime edits, follow `.cursor/rules/omni-cicd-k8s.mdc` (build → rollout → pytest → E2E when cluster exists).

---

*Next audit: re-run verify matrix, refresh dates, and append a one-line changelog under this header.*
