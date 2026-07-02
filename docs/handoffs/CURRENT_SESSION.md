# Current Session Handoff

## Iteration 11 (2026-07-02T19:05Z)
Fixed the iteration-10 leftover: wired `scripts/add_tenant_api_key.sh` into
`scripts/provision_fresh_tenant.py` via a new `provision_api_key()` subprocess call, run
automatically after the Postgres tenant row is created (`--skip-api-key` escape hatch added).
Verified live on the real cluster: single command `provision_fresh_tenant.py --tenant-id
tenant-wiretest-01` created both the Postgres row and the gateway API key + rolling-restarted
`omni-gateway`; idempotent re-run no-op'd both steps; gateway healthz `{"status":"ok"}` after
mutation; cleanup reverted `OMNI_TENANT_APIKEYS` to the original 3-tenant value and `DELETE`d the
disposable Postgres row, confirmed only 3 original tenants remain. `pytest -k "onboarding or
gateway_api or tenant" --ignore=tests/integration` → 146 passed. Full detail:
`docs/product/PRODUCT_PROOF.md` → "Iteration 11".

**Not done**: no automated pytest coverage yet for `provision_api_key()` — only live-cluster manual
verification. Iteration 9 leftovers still open (see below).

## Iteration 10 (2026-07-02T18:20Z)
Fixed the iteration-9 leftover: tenant API-key provisioning into `omni-gateway-secret`
(`OMNI_TENANT_APIKEYS`) was done by hand via `kubectl patch`. Added
`scripts/add_tenant_api_key.sh <tenant_id> [api_key]` — idempotent (no-op if tenant already
present), generates a key via `openssl rand -hex 32` if omitted, patches the secret, rolling
restarts + `rollout status` on `omni-gateway`. Verified live on the real cluster: no-op path against
existing `tenant-replay-01`, mutation + idempotent re-run against disposable
`tenant-scripttest-01`, then reverted the secret to its original 3-tenant value and confirmed gateway
healthz `{"status":"ok"}` + pod env still has `tenant-replay-01`. `pytest -k "onboarding or
gateway_api or tenant"` → 146 passed. Full detail: `docs/product/PRODUCT_PROOF.md` → "Iteration 10".

**Not done**: wiring the script into `scripts/provision_fresh_tenant.py` for a single-command flow
(still 2 separate steps). Iteration 9 leftovers still open (see below).

## Iteration 9 (2026-07-02T18:05Z)
Phase 6 of slice "Repeatable Tenant Onboarding Baseline" DONE at scoped level: installed a second
agent (`omni-remote-agent-replay01.service`) on VM `cust-edge` bound to `tenant-replay-01`, running
alongside the existing `staging-sim` agent. Runtime proof: register/evidence 200 OK, Twin
(`omni:aoip:system_model:tenant-replay-01`) has 41 facts/revision=6/only host `cust-edge`, isolation
confirmed vs `staging-sim` (Twin unchanged at 78 facts/3 hosts). `/onboarding/unknowns`,
`/onboarding/competency` verified live for the new tenant. One UX gap found (not a bug):
`resolve_scope()` silently overrides `tenant_id` for non-admin keys instead of returning 403 — noted
in `docs/product/PRODUCT_PROOF.md` → "Iteration 9", not fixed.

**Not done**: multi-host for `tenant-replay-01` (currently 1/1 host), automated test coverage for
"2 agents/2 tenants on 1 VM", `resolve_scope()` UX fix.

## Deliverable
Slice "Repeatable Tenant Onboarding Baseline" (Continuous Productization Loop) via skill
`.claude/skills/omni-autonomous-productizer/`. Goal: prove a fresh lab tenant can go through
Tenant→Agent→Discovery→Fact→Twin→Competency without manual intervention. Iterations 1-10 already
DONE (System Twin persistence, cust-app discovery, gateway/aoip import fix, Fact provenance fix,
evidence compaction, canonical provisioning module, skill bootstrap, tenant idempotency, fresh-tenant
Postgres row, second Agent + isolation proof, canonical API-key script).

## Current state
- Branch `main` @ `deb8ea8` (HEAD unchanged this iteration — only docs/scripts, not yet committed).
- Safety: `OMNI_AUTO_EXECUTE_ENABLED=false` verified live on `omni-fullstack` after this iteration's
  2 gateway rolling restarts (test cycle + revert). All core pods 1/1 Running.
- Next step: add pytest coverage for `provision_api_key()`, OR decide if Phase 6 needs multi-host
  before calling the slice's Phase 7 (repeatability + operator proof polish) DONE.
  Full detail in `docs/operations/AUTONOMOUS_LOOP_STATE.json` → `iteration.next_step`.

## Note on unrelated commits
`8d4c0ed` and `7689049` (provider-portal / lab-incidents work) landed on `main` outside this skill's
tracking — confirmed unrelated to onboarding/Twin/Competency/tenant-replay-01, not a bug, just
parallel work merged to `main`. Not re-litigated each iteration; see git log for detail if needed.

## Working tree
This iteration updated `scripts/provision_fresh_tenant.py` (added `provision_api_key()` +
`--skip-api-key`) and `docs/product/PRODUCT_PROOF.md`, `docs/operations/AUTONOMOUS_LOOP_STATE.json`,
`docs/operations/AUTONOMOUS_LOOP_LEDGER.md`, this file. Pre-existing unrelated modifications in
`docs/post-mortems/*.md` and `.autonomous-loop/` (supervisor runtime logs, not source) are untouched
by this iteration and not part of its commit.
