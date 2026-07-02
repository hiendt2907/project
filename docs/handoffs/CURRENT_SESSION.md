# Current Session Handoff

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
Tenant→Agent→Discovery→Fact→Twin→Competency without manual intervention. Iterations 1-9 already
DONE (System Twin persistence, cust-app discovery, gateway/aoip import fix, Fact provenance fix,
evidence compaction, canonical provisioning module, skill bootstrap, tenant idempotency, fresh-tenant
Postgres row, second Agent + isolation proof).

## Current state
- Branch `main` @ `00d7908` (HEAD unchanged this iteration — only docs/scripts, not yet committed).
- Safety: `OMNI_AUTO_EXECUTE_ENABLED=false` verified live on `omni-fullstack` after this iteration's
  3 gateway rolling restarts (test cycle + revert). All core pods 1/1 Running.
- Next step: wire `add_tenant_api_key.sh` into `provision_fresh_tenant.py`, OR decide if Phase 6
  needs multi-host before calling the slice's Phase 7 (repeatability + operator proof polish) DONE.
  Full detail in `docs/operations/AUTONOMOUS_LOOP_STATE.json` → `iteration.next_step`.

## Note on unrelated commits
`8d4c0ed` and `7689049` (provider-portal / lab-incidents work) landed on `main` outside this skill's
tracking — confirmed unrelated to onboarding/Twin/Competency/tenant-replay-01, not a bug, just
parallel work merged to `main`. Not re-litigated each iteration; see git log for detail if needed.

## Working tree
This iteration added `scripts/add_tenant_api_key.sh` (new, executable) and updated
`docs/product/PRODUCT_PROOF.md`, `docs/operations/AUTONOMOUS_LOOP_STATE.json`,
`docs/operations/AUTONOMOUS_LOOP_LEDGER.md`, this file. Pre-existing unrelated modifications in
`docs/post-mortems/*.md` and `.autonomous-loop/` (supervisor runtime logs, not source) are untouched
by this iteration and not part of its commit.
