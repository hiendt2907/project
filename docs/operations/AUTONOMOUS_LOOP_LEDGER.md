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

### Checkpoint 2026-07-02T19:40:00Z
- Timestamp: 2026-07-02T19:40:00Z
- Iteration: iter12-pytest-coverage-provision-api-key
- Quota state: N/A (not draining)
- HEAD: df5e753 (unchanged this iteration — test-only addition, not yet committed)
- Acceptance: PASSED
- Last verified: 2026-07-02T19:40:00Z
- Bottleneck: iteration 11's leftover — `provision_api_key()` in `scripts/provision_fresh_tenant.py`
  only had live-cluster manual proof, no CI-safe automated test.
- Fix: new `tests/test_provision_fresh_tenant.py` (3 tests) — mocks `subprocess.run` to assert the
  exact `bash add_tenant_api_key.sh <tenant_id>` invocation with `check=True` and correct `cwd`;
  asserts `CalledProcessError` propagates instead of being swallowed; asserts `ADD_API_KEY_SCRIPT`
  resolves to a real file. Read-only w.r.t. cluster — no live mutation this iteration (not needed;
  live path already runtime-proven in iteration 11).
- Runtime proof: reused iteration 11's live-cluster proof (unchanged this iteration) — omni-fullstack
  `OMNI_AUTO_EXECUTE_ENABLED=false` reconfirmed via `kubectl get deploy omni-fullstack -o jsonpath`,
  all core pods 1/1 Running (`kubectl get deploy,pod -n multi-agent`).
- Test suite: `.venv/bin/python -m pytest tests/test_provision_fresh_tenant.py -q` → 3 passed.
  `.venv/bin/python -m pytest -k "onboarding or gateway_api or tenant or provision"
  --ignore=tests/integration -q` → 155 passed (was 146 in iteration 11 baseline; +3 new +6 more
  matched by broadened `provision` keyword — no regressions).
- Pending: multi-host for tenant-replay-01, "2 agents/2 tenants" test coverage, resolve_scope() UX
  gap — all still open from iteration 9, unaffected by this iteration.
- Reset at: n/a
- Resume action: not yet committed — next step is `git add tests/test_provision_fresh_tenant.py
  docs/product/PRODUCT_PROOF.md docs/operations/AUTONOMOUS_LOOP_STATE.json
  docs/operations/AUTONOMOUS_LOOP_LEDGER.md docs/handoffs/CURRENT_SESSION.md` + commit, then decide
  next bottleneck (multi-host Phase 6 decision vs resolve_scope() UX fix).

### Checkpoint 2026-07-02T20:05:00Z
- Timestamp: 2026-07-02T20:05:00Z
- Iteration: iter13-two-agents-two-tenants-test-coverage
- Quota state: N/A (not draining)
- HEAD: 9710ffd (unchanged this iteration until commit below)
- Acceptance: PASSED
- Last verified: 2026-07-02T20:05:00Z
- Bottleneck: iteration 9's leftover — "2 agents/2 tenants on 1 VM" isolation (staging-sim +
  tenant-replay-01 both running Remote Agent instances on VM `cust-edge`) had only live-cluster
  manual proof (Twin fact counts inspected by hand), no automated regression test locking the
  behavior in.
- Fix: new `tests/test_onboarding_pipeline.py::TestTwoAgentsTwoTenantsOneVM` (2 tests) — drives the
  real `workers.onboarding_pipeline.accumulate_discovery_evidence()` pipeline (same entrypoint the
  live Kafka discovery-evidence worker uses) with two evidence envelopes sharing an identical
  `namespace`/hostname (`cust-edge`) but different `tenant_id`/`agent_id`, asserting: (1) each
  tenant's `aoip.system_model_store` Twin (`omni:aoip:system_model:{tenant_id}`) contains only its
  own facts despite the identical `host:cust-edge` subject, (2) the legacy flat-doc accumulation
  (`pkg.onboarding.discovery_doc`) also stays isolated per tenant, (3) Fact provenance never leaks
  the other tenant's `agent_id` tag. Confirms structurally why isolation holds:
  `system_model_store.MODEL_KEY` is `omni:aoip:system_model:{tenant_id}` (tenant_id-keyed Redis
  key), so two tenants sharing a hostname never share storage even though both fold a
  `host:cust-edge` subject into their own independent Twin.
- Runtime proof: no live-cluster mutation this iteration (test-only, root cause already understood
  structurally from code — `MODEL_KEY` format string — no ambiguity requiring live re-verification).
  Reconfirmed `OMNI_AUTO_EXECUTE_ENABLED=false` on `omni-fullstack` via `kubectl exec ... printenv`
  and all core Deployments Running via `kubectl get deploy,pod -n multi-agent -o wide`.
- Test suite: `.venv/bin/python -m pytest tests/test_onboarding_pipeline.py -q` → 29 passed (was 27).
  `.venv/bin/python -m pytest -k "onboarding or gateway_api or tenant or provision"
  --ignore=tests/integration -q` → 157 passed (was 155 — no regressions).
- Also investigated this iteration (no fix needed): `resolve_scope()` silent-override for non-admin
  callers is NOT a bug — `tests/test_tenant_isolation.py::TestResolveScope::test_non_admin_ignores_override`
  and `TestKpiTenantIsolation::test_non_admin_cannot_scope_override` already lock this in as the
  intentional, tested contract (non-admin tenant_id query param is silently ignored, not honored —
  changing to 403 would be a breaking behavior change to a settled design, not a bug fix). Downgrading
  this from "open UX gap" to "confirmed intentional, no action" in `current-priority.md`.
- Pending: multi-host for tenant-replay-01 (still 1/1 host) — the only remaining item from iteration
  9's leftovers list.
- Reset at: n/a
- Resume action: stage `tests/test_onboarding_pipeline.py`, `docs/product/PRODUCT_PROOF.md`,
  `docs/operations/AUTONOMOUS_LOOP_STATE.json`, `docs/operations/AUTONOMOUS_LOOP_LEDGER.md`,
  `docs/handoffs/CURRENT_SESSION.md`, `.claude/skills/omni-autonomous-productizer/references/current-priority.md`
  and commit. Next bottleneck: multi-host for `tenant-replay-01` (Phase 6 decision before the slice
  "Repeatable Tenant Onboarding Baseline" can be called fully DONE).

### Checkpoint 2026-07-02T21:15:00Z
- Timestamp: 2026-07-02T21:15:00Z
- Iteration: iter14-tenant-replay-01-multihost
- Quota state: n/a
- HEAD: 83013c2 (pre-commit; this checkpoint's changes not yet committed)
- Acceptance: PASS
- Last verified: iteration 9's last remaining leftover — `tenant-replay-01` only had 1/1 host
  (`cust-edge`), no proof of a single-tenant Twin merging facts from multiple distinct hosts.
  Installed a real second Remote Agent for `tenant-replay-01` on VM `cust-app`
  (`/opt/omni-remote-agent-replay01/`, systemd unit `omni-remote-agent-replay01.service`, reusing
  the existing gateway API key for `tenant-replay-01`), alongside the pre-existing `staging-sim`
  agent already on that VM. Runtime proof: agent log shows register/profile/evidence all 200 OK;
  Redis `omni:aoip:system_model:tenant-replay-01` revision 54→66, facts now span
  `{cust-edge, cust-app}` (was `{cust-edge}` only); `staging-sim` Twin on the same shared VM
  unaffected (`{cust-edge, cust-db, cust-app}`/76 facts, unchanged); `GET
  /onboarding/competency?entity_type=host&entity_id=host:cust-app` with tenant-replay-01's bearer
  token returns live VERIFIED facets sourced from `agent:tenant-replay-01_cust-app`. Added
  `tests/test_onboarding_pipeline.py::TestOneTenantTwoHosts` (2 tests) exercising
  `accumulate_discovery_evidence()` with two envelopes (same tenant_id, different
  namespace/agent_id) and asserting Twin merge + per-host provenance isolation.
  `.venv/bin/python -m pytest tests/test_onboarding_pipeline.py -q` → 31 passed (was 29). Regression
  `-k "onboarding or gateway_api or tenant or provision" --ignore=tests/integration -q` → 159 passed
  (was 157). Reconfirmed `OMNI_AUTO_EXECUTE_ENABLED=false` on `omni-fullstack` post-change (VM agent
  install is a read-only-evidence change, no K8s mutation/executor/CRAT path touched).
- Reset at: n/a
- Resume action: stage `tests/test_onboarding_pipeline.py`, `docs/product/PRODUCT_PROOF.md`,
  `docs/operations/AUTONOMOUS_LOOP_STATE.json`, `docs/operations/AUTONOMOUS_LOOP_LEDGER.md`,
  `docs/handoffs/CURRENT_SESSION.md`,
  `.claude/skills/omni-autonomous-productizer/references/current-priority.md` and commit. All
  iteration-9 leftovers are now closed; next bottleneck TBD at next iteration start (candidates:
  `cust-db` agent for `tenant-replay-01` for full 3/3 host parity with `staging-sim`, or move to
  Phase 6/7 items — UnderstandingComplete, Handover, operator portal UI).

### Checkpoint 2026-07-02T22:10:00Z
- Iteration: iter15-human-claim-runtime-verify
- Quota state: n/a
- HEAD: a7c6847 (iteration 14 confirmed already committed — prior state.json was stale/said
  "not yet committed"; corrected)
- Acceptance: PASS
- Last verified: PRODUCT_PROOF.md row 28 said Unknown/Question/Human-Claim lifecycle (O2B) was
  never runtime-verified ("chưa kiểm trong iteration 1") despite having code + unit tests. Via
  `kubectl port-forward svc/omni-gateway 18090:80`, fetched real PENDING questions for tenant
  `staging-sim`, found `bdb9bb5e66be555d1fd3dd80` (`svc:nginx`, facet `business_capability`).
  Answered via `POST /onboarding/questions/{id}/answer` → 200 OK, `answer_id=a8ddaa6bd49e2f83b9cb`,
  question flips PENDING→ANSWERED (re-fetched, confirmed). `GET
  /onboarding/competency?entity_type=service&entity_id=svc:nginx` then shows facet
  `business_capability: state=CLAIMED, evidence_refs=[human:iter15-productizer,
  question:bdb9bb5e66be555d1fd3dd80]` — correctly stays CLAIMED not VERIFIED (no matching machine
  Fact), confirming the Claim-vs-Fact promotion contract holds on the real Twin. Gap found (not
  fixed): `compute_business_flow_pct()` (`src/pkg/onboarding/discovery_doc.py:198-203`) — one of 3
  conditions in the `UnderstandingComplete`/readiness gate — reads
  `service_topology.services[].described` (machine-set only) and is fully disconnected from
  `competency_matrix`/`question_lifecycle`; answering every open Question for a tenant does not move
  `readiness_flag` toward `true`. Documented as next bottleneck candidate, not fixed this iteration
  (needs a design decision on how readiness should read Claim coverage). No source code changed —
  ran `tests/test_aoip_question_lifecycle.py tests/test_gateway_onboarding_competency_routes.py -q`
  → 19 passed (no regression). No K8s mutation this iteration.
- Reset at: n/a
- Resume action: stage `docs/product/PRODUCT_PROOF.md`, `docs/operations/AUTONOMOUS_LOOP_STATE.json`,
  `docs/operations/AUTONOMOUS_LOOP_LEDGER.md`, `docs/handoffs/CURRENT_SESSION.md`,
  `.claude/skills/omni-autonomous-productizer/references/current-priority.md` and commit
  (docs/runtime-verification only, no source change). Next bottleneck candidates for iteration 16:
  (a) wire `competency_matrix` coverage into the readiness gate so Human Claims can actually reach
  `UnderstandingComplete=true`, (b) runtime-verify the Handover-doc path (`POST
  /onboarding/handover-doc`, A8) which has code+unit tests but no Continuous-Productization-Loop
  runtime proof yet, (c) `cust-db` agent for `tenant-replay-01` (3/3 host parity, lower priority).

### Checkpoint 2026-07-02T23:45:00Z
- Timestamp: 2026-07-02T23:45:00Z
- Iteration: iter16-handover-doc-runtime-verify
- Status: DONE
- Summary: Runtime-verified `POST /onboarding/handover-doc` (A8) against the real cluster, closing
  the last leftover named in iteration 15. Via pre-existing `kubectl port-forward svc/omni-gateway
  18080:80`, captured `staging-sim`'s diagram version before (`version=6747`), POSTed a real
  handover doc using the tenant's real bearer key from `omni-gateway-secret` → `200 OK`,
  `diagram_version=6752` (bump proves `dd.accumulate_probe_fact()`+`dd.regenerate_diagrams()` ran
  on the real Redis pipeline, not a canned response). `GET /onboarding/doc` afterward shows
  `doc_snapshot.documents=[{path:"iter16-runbook.md", content_hash:"5429b992...",
  content_length:124}]` — no raw content field, confirming `INV_DATA_RESIDENCY` holds on the real
  pipeline. Readiness unchanged (`readiness_flag=false`) as expected — unrelated mechanism, same
  known gap as iteration 15. No source code changed — ran `pytest tests/test_onboarding_pipeline.py
  -q -k handover` → 3 passed (no regression). No K8s mutation this iteration;
  `OMNI_AUTO_EXECUTE_ENABLED=false` reconfirmed via `kubectl exec`.
- Reset at: n/a
- Resume action: stage `docs/product/PRODUCT_PROOF.md`, `docs/operations/AUTONOMOUS_LOOP_STATE.json`,
  `docs/operations/AUTONOMOUS_LOOP_LEDGER.md`, `docs/handoffs/CURRENT_SESSION.md`,
  `.claude/skills/omni-autonomous-productizer/references/current-priority.md` and commit
  (docs/runtime-verification only, no source change). Next bottleneck candidates for iteration 17:
  (a) design + wire `competency_matrix` coverage into `compute_business_flow_pct()`/readiness gate
  (still the highest-value open golden-journey link, carried over from iteration 15), (b) `cust-db`
  agent for `tenant-replay-01` (3/3 host parity, lowest priority), (c) operator portal UI for
  competency/unknowns/diagram (currently API-only).

### Checkpoint 2026-07-03T09:22:18+07:00
- Timestamp: 2026-07-03T09:22:18+07:00 (commit `cf11f1f`)
- Iteration: iter17-readiness-gate-competency-wiring
- Status: DONE (VERIFIED_RUNTIME)
- Summary: Closed the highest-value carry-over from iteration 15/16 — `compute_business_flow_pct()`
  (`src/pkg/onboarding/discovery_doc.py`) only read `service_topology.services[].described`
  (machine-set), so answering every open Question via O2B never moved `business_flow_confirmed_pct`
  or `readiness_flag`. Design decision: a service now counts as "confirmed" if EITHER `described`
  is true OR the Entity Competency Matrix reports a CLAIMED/VERIFIED `business_capability` facet.
  Signature changed to `compute_business_flow_pct(redis, tenant_id, doc)` (async); only caller was
  already-async `compute_readiness()`, no breaking change. `pytest tests/test_onboarding_pipeline.py
  -q` → 32 passed (new `TestReadinessThresholds::test_answered_human_claim_counts_toward_business_flow_pct`).
  Full suite `pytest tests/ -q --ignore=tests/integration` → 5956 passed, 1 pre-existing flake
  (`test_register_then_real_system_metrics_emitted_through_real_pipeline`, unrelated — depends on
  real-machine z-score at test time). Built + deployed `multi-agent-system:latest` and
  `omni-gateway:latest`, rolled out `omni-fullstack`/`omni-onboarding`/`omni-gateway`. Confirmed new
  signature live in running pod via `kubectl exec`. Runtime-verified end-to-end against real Redis
  on a disposable scratch tenant (`iter17-readiness-proof`, keys deleted after):
  `business_flow_confirmed_pct` 0.0→100.0, `readiness_flag` False→True from an answered Claim alone.
  Both real lab tenants (`staging-sim`, `tenant-replay-01`) unaffected by the deploy.
  `OMNI_AUTO_EXECUTE_ENABLED=false` reconfirmed throughout. Full detail: `docs/product/PRODUCT_PROOF.md`
  → "Iteration 17".
- Reset at: n/a
- Resume action: this ledger entry + `AUTONOMOUS_LOOP_STATE.json` were the only two files not yet
  updated for iteration 17 (`PRODUCT_PROOF.md` and `current-priority.md` were already current) —
  backfilled 2026-07-03 during a tech-debt sweep, no new source change. Next bottleneck candidates:
  (a) `cust-db` agent for `tenant-replay-01` (3/3 host parity, lowest priority), (b) operator portal
  UI for competency/unknowns/diagram (currently API-only).

### Checkpoint 2026-07-03T12:30:00+07:00
- Timestamp: 2026-07-03T12:30:00+07:00
- Iteration: iter18-phase1-contract-freeze
- Status: DONE (VERIFIED_RUNTIME)
- Summary: Phase-1 Product & Architecture Contract Freeze (production productization plan).
  Tạo `docs/product/PRODUCT_CONTRACT.md` (Golden Journey chính thức, 3-action remediation catalog,
  5 hard-zero SLO, tier gates, non-goals) và `docs/architecture/ADR-002-command-protocol.md`.
  Phát hiện quan trọng: hướng ADR-001 §5 (gateway import `DurableCommandChannel`) là sai chiều —
  `agent_runtime.py` đã vượt bản aoip về an toàn (fencing/atomic Lua claim/heartbeat);
  `DurableCommandChannel` là legacy có sunset criteria. Tạo `src/aoip/protocol/` — nguồn chân lý
  duy nhất cho command state vocabulary; `agent_runtime.py` + `delivery.py` import chung
  (refactor import-only); contract test `tests/test_aoip_protocol_contract.py` (13 test) parse Lua
  source chặn drift bảng TERMINAL. `requirements.lock` tạo mới (Dockerfile chưa wire —
  TECH_DEBT_BACKLOG #13). Full suite 5965 passed / 0 failed. Runtime proof: rebuild+rollout
  omni-gateway (`f9ccdf1fe277…`) + multi-agent-system (`bfa8fe4b053f…`), kubectl exec xác minh
  identity-share của TERMINAL_STATES trong cả gateway lẫn fullstack pod, `/readyz` 200,
  `OMNI_AUTO_EXECUTE_ENABLED=false` reconfirmed.
- Reset at: n/a
- Resume action: Phase 2 — Golden Journey Read-only qua official API/portal (ứng viên đầu:
  operator portal UI cho competency/unknowns, hiện API-only).

### Checkpoint 2026-07-03T14:30:00+07:00
- Timestamp: 2026-07-03T14:30:00+07:00
- Iteration: iter19-operator-understanding-surface
- Status: DONE (VERIFIED_RUNTIME)
- Summary: Phase-2 Golden Journey Read-only, slice 1 — Operator Understanding surface. Gateway
  thêm `GET /onboarding/entities` (entity index của System Twin, 3 test TDD mới); omni-ui thêm
  trang `/understanding` (readiness + entity list + Competency Matrix facet table + Open Unknowns
  + Questions, TenantSelector, honest per-section error, không mock fallback) qua 2 Next proxy
  route mới; sidebar link cả 3 realm nav. Fix phụ: root `ui/tsconfig.json` exclude `apps/packages`
  (pre-existing latent type-check break của provider-portal chặn `next build` root —
  TECH_DEBT_BACKLOG #14). Full suite 5967 passed / 1 known flake. Runtime proof: rebuild+rollout
  omni-gateway (`aa24b92ad3bf…`) + omni-ui (`b0c85bbdd6d7…`); `/onboarding/entities` trả 3/3 host
  + 7 service thật (rev 2793→2814 realtime); UI proof CÓ AUTH THẬT (NextAuth login qua Host
  omni.ai-agent.local): aggregate API trả 352 unknowns/336 questions/readiness_flag=true,
  competency host:cust-app facet thật (identity VERIFIED 0.85, process CONTRADICTED, coverage 50%),
  page HTML 200; unauthenticated → 401. Kill-switch false + /readyz 200 reconfirmed.
  Chi tiết: `docs/product/PRODUCT_PROOF.md` → "Iteration 19".
- Reset at: n/a
- Resume action: Phase 2 slice kế tiếp — (a) nút Answer question trên UI (write-action đầu tiên
  của portal, API đã runtime-verified iter 15), hoặc (b) render Mermaid diagram trên trang
  Understanding, hoặc (c) Playwright E2E cho trang mới. Không mở action/billing song song
  (PRODUCT_CONTRACT §9).
