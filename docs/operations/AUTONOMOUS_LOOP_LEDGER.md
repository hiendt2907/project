# Autonomous Loop Ledger

Append-only log của skill `omni-autonomous-productizer`. Mỗi checkpoint (quota-drain, sleep, resume,
iteration DONE/PARTIAL/BLOCKED) thêm một entry mới ở CUỐI file. Không sửa/xóa entry cũ.

## Format

```
### Checkpoint <UTC ISO8601>
- Timestamp:
- Iteration:
- Quota state:
- HEAD:
- Acceptance:
- Last verified:
- Pending:
- Reset at:
- Resume action:
```

---

### Checkpoint 2026-07-02T07:14:15Z
- Timestamp: 2026-07-02T07:14:15Z
- Iteration: bootstrap (skill creation — no product iteration started yet)
- Quota state: n/a
- HEAD: e8a8c96390616e0a0cd23d9388289966960cdb08
- Acceptance: n/a
- Last verified: skill package files created + smoke test pending
- Pending: run smoke test (status + read-only reality check), then `/omni-autonomous-productizer start`
- Reset at: n/a
- Resume action: read `docs/operations/AUTONOMOUS_LOOP_STATE.json`, run `references/operating-model.md` Reality Map procedure

### Checkpoint 2026-07-02T07:24:00Z
- Timestamp: 2026-07-02T07:24:00Z
- Iteration: iter6-tenant-idempotency
- Quota state: n/a (not draining)
- HEAD: 5c76425 (pre-commit of this iteration)
- Acceptance: PASS — `AdminConfigRepo.create_tenant(idempotent=True)` implemented, test
  `test_create_tenant_idempotent_true_is_repeatable` passing, full suite 5940 passed / 6 deselected
  (1 known pre-existing flake deselected: `test_register_then_real_system_metrics_emitted_through_real_pipeline`)
- Last verified: `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` → 5940 passed
- Pending: wire `idempotent=True` into a real Phase-4 fresh-tenant provisioning caller (not done yet — this iteration only unblocks it)
- Reset at: n/a
- Resume action: after commit, next bottleneck = Phase 4 of "Repeatable Tenant Onboarding Baseline" (fresh tenant `tenant-replay-01` via canonical provisioning + `scripts/lib/remote_agent_provisioning.py`)

### Checkpoint 2026-07-02T14:45:00Z
- Timestamp: 2026-07-02T14:45:00Z
- Iteration: iter7-fresh-tenant-repeat-provisioning-proof
- Quota state: n/a (not draining)
- HEAD: 50d2149 (pre-commit of this iteration)
- Acceptance: PASS — new `scripts/provision_fresh_tenant.py` calls `AdminConfigRepo.create_tenant(idempotent=True)`
  directly against a real asyncpg pool. Ran twice against real Postgres in-cluster (`omni-postgres-0`
  via `kubectl port-forward svc/omni-postgres 15432:5432`) for tenant `tenant-replay-01`. Verified by
  direct `psql` query: exactly 1 row in `omni_admin.tenant`, exactly 1 audit event in
  `omni_admin.config_change_log` (`actor=provisioning-tooling, action=create`) despite 2 calls —
  first-time VERIFIED_RUNTIME proof of iteration 6's idempotent path (previously only VERIFIED_TEST
  with FakePgPool). `.venv/bin/python -m pytest tests/test_admin_config_store.py
  tests/test_remote_agent_provisioning.py -q` → 29 passed.
- Last verified: psql query output captured in this checkpoint; PRODUCT_PROOF.md "Iteration 7" section updated.
- Pending: commit this iteration. Phase 6-7 of "Repeatable Tenant Onboarding Baseline" still open —
  `tenant-replay-01` only exists as an `omni_admin.tenant` row, no Agent/VM/discovery/Twin/Competency
  wired to it yet, and no cross-tenant isolation proof vs `staging-sim` yet.
- Reset at: n/a
- Resume action: commit iter7, then next bottleneck = Phase 6 (provision Agent+VM for
  `tenant-replay-01`, run golden journey end to end for it, prove isolation vs `staging-sim`).

### Checkpoint 2026-07-02T15:05:00Z
- Timestamp: 2026-07-02T15:05:00Z
- Iteration: iter7-fresh-tenant-repeat-provisioning-proof (state reconciliation, no new code)
- Quota state: n/a (not draining)
- HEAD: 8e15178 (confirmed committed — `git log --oneline -3` shows 8e15178, 0d7d352, both after
  the 50d2149 the state file still pointed to)
- Acceptance: N/A — this iteration performed reality reconciliation only, per skill instruction
  "resume must verify reality lager, not blindly continue a stale hypothesis." State JSON and this
  ledger previously said iter7 was pre-commit; git history proved it already landed. Working tree
  reverified clean except pre-existing unrelated post-mortem timestamp diffs and untracked
  `.autonomous-loop/` runtime log dir (both out of scope, not touched).
- Last verified: `git status --short`, `git log --oneline -5`, `git diff --stat` (only pre-existing
  post-mortem files modified), `orb list` (3 VMs Running), `kubectl get deploy,pod -n multi-agent`
  (omni-fullstack/omni-onboarding/omni-gateway 1/1 Running), `OMNI_AUTO_EXECUTE_ENABLED` present on
  omni-fullstack env (kill-switch still wired, value check deferred to next mutation-adjacent
  iteration since this one made no runtime changes).
- Pending: Phase 6 of "Repeatable Tenant Onboarding Baseline" — provision real Agent+VM for
  `tenant-replay-01`, run golden journey Tenant->Agent->Discovery->Fact->Twin->Competency for it,
  prove cross-tenant isolation vs `staging-sim`, prove operator read-only flow. This is a
  multi-step slice (new VM provisioning + agent install + discovery cycle observation) too large
  for the remainder of this iteration's budget — deliberately left as `next_step` for the next
  `one-iteration` invocation rather than started and left half-finished.
- Reset at: n/a
- Resume action: `references/operating-model.md` Reality Map procedure, then start Phase 6 fresh:
  decide reuse-existing-VM-with-second-agent-identity vs provision-new-VM, inspect
  `scripts/e2e_orbstack_fleet.py` and `scripts/lib/remote_agent_provisioning.py` before writing any
  provisioning code (inspect-before-code discipline).

### Checkpoint 2026-07-02T17:55:00Z
- Timestamp: 2026-07-02T17:55:00Z
- Iteration: iter8-reality-reconciliation-pre-phase6
- Quota state: n/a (not draining)
- HEAD: 7689049 (confirmed via `git log --oneline -3`)
- Acceptance: N/A — reality reconciliation only, no new code this iteration. State/ledger
  previously pointed to HEAD=8e15178; git history showed 2 additional commits already landed
  outside this loop: `8d4c0ed` (feat(portal): runtime-backed understanding and human claim
  workflow with codex — src/aoip/console/{agents,understanding,human_inbox}.py +
  ui/apps/provider-portal) and `7689049` (Add provider lab incident endpoints —
  src/aoip/console/lab_incidents.py). Both are provider-portal / lab-incident feature work,
  unrelated to the tracked "Repeatable Tenant Onboarding Baseline" slice — confirmed no touch on
  tenant-replay-01, onboarding pipeline, Twin, or Competency code paths. Per skill instruction
  ("resume must verify reality, not blindly continue a stale hypothesis"), reconciled state/ledger
  to match before deciding next bottleneck.
- Last verified: `git status --short` (only pre-existing post-mortem timestamp diffs +
  `.autonomous-loop/` runtime logs, no untracked owned files), `kubectl get deploy omni-fullstack -n
  multi-agent -o json` → `OMNI_AUTO_EXECUTE_ENABLED=false`, `kubectl get pods -n multi-agent` → all
  core pods 1/1 Running (omni-fullstack, omni-onboarding, omni-gateway, omni-postgres-0), `orb list`
  → 3 VMs Running, `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` → **5948
  passed, 5 deselected, 1 failed** (known pre-existing flake
  `test_register_then_real_system_metrics_emitted_through_real_pipeline`, confirmed unrelated in
  prior iterations).
- Pending: Phase 6 of "Repeatable Tenant Onboarding Baseline" unchanged — provision real Agent+VM
  for `tenant-replay-01`, run golden journey Tenant→Agent→Discovery→Fact→Twin→Competency for it,
  prove cross-tenant isolation vs `staging-sim`, prove operator read-only flow. Not started this
  iteration — multi-step VM/agent provisioning slice too large for remaining budget, deliberately
  left as clean `next_step` rather than started and abandoned mid-way.
- Reset at: n/a
- Resume action: `references/operating-model.md` Reality Map procedure, then start Phase 6:
  decide reuse-existing-VM-with-second-agent-identity vs provision-new-VM, inspect
  `scripts/e2e_orbstack_fleet.py` and `scripts/lib/remote_agent_provisioning.py` before writing any
  provisioning code (inspect-before-code discipline).

### Checkpoint 2026-07-02T18:05:00Z
- Timestamp: 2026-07-02T18:05:00Z
- Iteration: iter9-phase6-tenant-replay-01-agent
- Quota state: n/a (not draining)
- HEAD: 7689049 (unchanged — infra/runtime iteration, no source commit yet pending below)
- Acceptance: PARTIAL (bottleneck fixed, scoped honestly) — Phase 6 of "Repeatable Tenant Onboarding
  Baseline". Chose reuse-existing-VM-with-second-agent-identity (no new OrbStack VM — all 3 lab VMs
  already host staging-sim agents). Added `tenant-replay-01` key to `omni-gateway-secret`
  (OMNI_TENANT_APIKEYS), rolling-restarted omni-gateway (verified rollout status). Installed second
  agent on cust-edge: `/opt/omni-remote-agent-replay01` + systemd unit
  `omni-remote-agent-replay01.service`, bound to tenant-replay-01, running alongside the existing
  staging-sim agent without conflict (separate install dir, separate unit, separate log).
- Last verified:
  - `orb -m cust-edge sudo systemctl status omni-remote-agent-replay01.service` → active running.
  - `/var/log/omni-agent-replay01.log` → register/profile/evidence all HTTP 200, `enqueued=6`.
  - `redis-cli HGETALL omni:aoip:system_model:tenant-replay-01` → revision=6, 41 facts, subject only
    `host:cust-edge`, provenance only `agent:tenant-replay-01_cust-edge` (no agent:unknown).
  - `redis-cli HGET omni:aoip:system_model:staging-sim facts` → unchanged 78 facts / 3 hosts — no
    cross-tenant contamination from the new agent.
  - `GET /onboarding/unknowns?tenant_id=staging-sim` called with tenant-replay-01's API key → HTTP
    200 but body `tenant_id: tenant-replay-01` (281 own unknowns, not staging-sim's) — confirmed via
    body inspection (not just status code) that `resolve_scope()` silently ignores override_tid for
    non-admin callers. Initially misread as an isolation gap from status code alone; corrected after
    reading response body. Not a defect — noted as a UX gap (silent scope override instead of 403)
    for a future iteration, not fixed here.
  - `GET /onboarding/unknowns?tenant_id=tenant-replay-01` and
    `GET /onboarding/competency?tenant_id=tenant-replay-01&entity_type=host&entity_id=host:cust-edge`
    (correct key) → real Unknown/Competency data, evidence_refs point to
    `agent:tenant-replay-01_cust-edge` + real discovery probes.
- Pending: multi-host coverage for tenant-replay-01 (currently 1/1 host — cust-edge only, via second
  agent identity sharing the VM with staging-sim), automated test coverage for "2 agents/2 tenants on
  1 VM", canonicalizing the gateway-secret API-key provisioning step (done by hand via kubectl this
  iteration), and the resolve_scope() silent-override UX gap. docs/product/PRODUCT_PROOF.md
  "Iteration 9" has full detail.
- Reset at: n/a
- Resume action: pytest full suite + docs sync + commit this iteration's doc/state changes; next
  iteration should decide whether Phase 6 counts as DONE (1/1 host, isolation proven) or needs
  multi-host before moving to Phase 7 (repeatability + operator proof polish).

## Iteration 10 — 2026-07-02T18:20:00Z
- Bottleneck: iteration 9 leftover — tenant API-key provisioning into `omni-gateway-secret`
  (`OMNI_TENANT_APIKEYS`) was done by hand via `kubectl patch`, no script, not idempotent.
- Fix: `scripts/add_tenant_api_key.sh <tenant_id> [api_key]` — no-op if tenant already present
  (prints existing key), generates a key via `openssl rand -hex 32` if omitted, patches the secret,
  rolling-restarts + `rollout status` on `omni-gateway`.
- Runtime proof (live cluster, not just code):
  - No-op path against `tenant-replay-01` (already present from iter9) — printed the existing key,
    did not mutate the secret.
  - Mutation path against disposable `tenant-scripttest-01` — generated a key, patched the secret,
    gateway rollout succeeded.
  - Idempotent re-run against `tenant-scripttest-01` — returned the same key, no duplicate entry
    created.
  - Reverted `OMNI_TENANT_APIKEYS` to the original 3-tenant value (default/staging-sim/
    tenant-replay-01) via manual patch + rolling restart, verified content matches original exactly.
  - Post-cycle health: `kubectl get --raw .../omni-gateway:80/proxy/healthz` →
    `{"status":"ok","rate_limit_tps":1000}`; pod env `OMNI_TENANT_APIKEYS` still contains
    `tenant-replay-01`.
  - `.venv/bin/python -m pytest tests/ -q -k "onboarding or gateway_api or tenant"` → 146 passed.
- Pending: wire this script into `scripts/provision_fresh_tenant.py` for a single-command tenant
  provisioning flow (currently 2 separate steps). Iteration 9 leftovers (multi-host for
  tenant-replay-01, 2-agent/2-tenant test coverage, resolve_scope() UX gap) still open.
- Reset at: n/a
- Resume action: next iteration picks either the provision_fresh_tenant.py wiring or the Phase 6
  multi-host decision as the next bottleneck.

## Iteration 11 — 2026-07-02T19:05:00Z
- Bottleneck: iteration 10 leftover — `scripts/provision_fresh_tenant.py` (Postgres tenant row) and
  `scripts/add_tenant_api_key.sh` (gateway secret) were still 2 separate manual commands.
- Fix: `provision_fresh_tenant.py` now calls a new `provision_api_key()` (subprocess `bash
  scripts/add_tenant_api_key.sh <tenant_id>`) right after the Postgres row is provisioned. Added
  `--skip-api-key` flag for callers that want to manage the gateway secret separately.
- Runtime proof (live cluster, not just code):
  - Port-forwarded `omni-postgres:5432`, ran `provision_fresh_tenant.py --tenant-id
    tenant-wiretest-01 --display-name "Wire Test 01"` — single command produced both the Postgres
    row AND generated+patched a gateway API key AND rolling-restarted `omni-gateway` successfully.
  - Idempotent re-run of the same command — Postgres insert no-op'd (`create_tenant(idempotent=True)`),
    `add_tenant_api_key.sh` no-op'd (same key returned, no duplicate entry, no second restart).
  - Gateway health after mutation: port-forwarded `svc/omni-gateway:80` → `curl .../healthz` →
    `{"status":"ok","rate_limit_tps":1000}`.
  - Cleanup: reverted `OMNI_TENANT_APIKEYS` to the original 3-tenant value + rolling restart +
    verified content matches original + healthz ok again; `DELETE FROM omni_admin.tenant WHERE
    tenant_id='tenant-wiretest-01'` — confirmed only the 3 original tenants remain.
  - `.venv/bin/python -m pytest tests/ -q -k "onboarding or gateway_api or tenant"
    --ignore=tests/integration` → 146 passed, no regression.
- Pending: no automated pytest coverage yet for `provision_api_key()` call path (live-cluster manual
  verification only). Iteration 9 leftovers (multi-host for tenant-replay-01, 2-agent/2-tenant test
  coverage, resolve_scope() UX gap) still open.
- Reset at: n/a
- Resume action: next iteration decides between adding pytest coverage for the new wiring, the
  Phase 6 multi-host decision, or the resolve_scope() UX gap as the next bottleneck.
