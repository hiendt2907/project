# Current Session Handoff

## Iteration 17 (2026-07-02, IN PROGRESS — not yet committed)
Bottleneck: iteration 15's design-decision gap — `compute_business_flow_pct()` in
`src/pkg/onboarding/discovery_doc.py` only read `service_topology.services[].described`
(machine-set), so answering a Human Claim question (O2B) never moved a tenant's
`business_flow_confirmed_pct` / `readiness_flag`.

Design decision made: a service counts as "confirmed" if EITHER the discovery-doc
`described` flag is true OR the Entity Competency Matrix (`aoip.competency_matrix`) reports
a CLAIMED/VERIFIED `business_capability` facet for it (i.e. a Human Claim answered via O2B).

Change: `compute_business_flow_pct()` signature changed from
`compute_business_flow_pct(doc)` (sync) to `compute_business_flow_pct(redis, tenant_id, doc)`
(async) — only caller was `compute_readiness()` (already async, already awaited), so this is
not a breaking change for any other code path (confirmed via grep — only one call site
existed). Implementation loads `SystemModel` + contradictions + claims once via
`aoip.system_model_store`/`aoip.claims_store`, then projects `business_capability` per
service via `aoip.competency_matrix.build_entity_competency()` (pure/derived, no LLM).

Tests: new `TestReadinessThresholds::test_answered_human_claim_counts_toward_business_flow_pct`
in `tests/test_onboarding_pipeline.py` proves a service with no doc description but an
answered Claim reaches `business_flow_confirmed_pct == 100.0`. `pytest
tests/test_onboarding_pipeline.py -q` → 32 passed (was 31). Regression `-k "onboarding or
gateway_api or tenant or provision or competency or claim" --ignore=tests/integration` → 203
passed. Full `pytest tests/ -q --ignore=tests/integration` was launched in background to
confirm no other regression before this iteration is marked DONE — **not yet confirmed
finished as of this checkpoint**.

**Not yet done this iteration**: (1) wait for/confirm the full test-suite background run
green, (2) no deploy/runtime-verification against the live cluster performed yet — this is a
real behavior change (not docs-only) and per the skill's Definition of Done needs a rebuild +
redeploy of `omni-fullstack`/`omni-gateway` (whichever imports `discovery_doc.py`) plus a
runtime proof (e.g. answer a real Question for `staging-sim` or `tenant-replay-01` and observe
`business_flow_confirmed_pct` move) before this can be labeled DONE and committed. (3)
PRODUCT_PROOF.md / current-priority.md / AUTONOMOUS_LOOP_STATE.json / ledger not yet updated
for this iteration.

**Next step for a fresh session**: check whether the background `pytest tests/ -q
--ignore=tests/integration` finished green (rerun it if the background shell is gone), then
continue with build/deploy/runtime-verification per the plan above, then update
PRODUCT_PROOF.md/state/ledger/current-priority.md and commit only after runtime proof.

## Iteration 16 (2026-07-02T23:45Z)
Closed the other leftover named in iteration 15: runtime-verified `POST /onboarding/handover-doc`
(A8) against the real cluster. Via `kubectl port-forward svc/omni-gateway 18080:80`, captured
`staging-sim`'s diagram version before (`6747`), POSTed a real handover doc using the tenant's real
bearer key → `200 OK`, `diagram_version=6752` (bump proves the real accumulation+diagram pipeline
ran). `GET /onboarding/doc` afterward shows only `content_hash`/`content_length` for the uploaded
doc — no raw content — confirming the route's `INV_DATA_RESIDENCY` docstring claim holds on the real
pipeline, not just in the docstring. No source changed; `pytest tests/test_onboarding_pipeline.py -q
-k handover` → 3 passed. No K8s mutation. Full detail: `docs/product/PRODUCT_PROOF.md` →
"Iteration 16".

**Not done**: readiness-gate/competency wiring (iteration 15's design-decision gap — still the
highest-value open golden-journey link, not addressed here or in iteration 15); `cust-db` agent for
`tenant-replay-01` (3/3 host parity, lowest priority); operator portal UI.

## Iteration 15 (2026-07-02T22:10Z)
Closed the last "chưa kiểm" gap in PRODUCT_PROOF.md row 28: Unknown/Question/Human-Claim lifecycle
(O2B) had code + unit tests but was never runtime-verified. Via `kubectl port-forward
svc/omni-gateway 18090:80`, fetched a real PENDING question for tenant `staging-sim`
(`bdb9bb5e66be555d1fd3dd80`, `svc:nginx`, facet `business_capability`), answered it via `POST
/onboarding/questions/{id}/answer` → `200 OK`, question flips PENDING→ANSWERED. `GET
/onboarding/competency?entity_type=service&entity_id=svc:nginx` then shows facet
`business_capability: state=CLAIMED` with `evidence_refs=[human:iter15-productizer,
question:...]` — correctly NOT auto-promoted to VERIFIED (no matching machine Fact), confirming
the Claim-vs-Fact contract holds on the real Twin.

**Gap found (documented, not fixed)**: the `UnderstandingComplete`/readiness gate
(`compute_business_flow_pct()` in `src/pkg/onboarding/discovery_doc.py`) reads
`service_topology.services[].described` (machine-set only) and is disconnected from
`competency_matrix`/Human Claims — answering every open Question does not move a tenant's
`readiness_flag` toward `true`. Needs a design decision before fixing. No source changed this
iteration; `pytest tests/test_aoip_question_lifecycle.py
tests/test_gateway_onboarding_competency_routes.py -q` → 19 passed. No K8s mutation. Full detail:
`docs/product/PRODUCT_PROOF.md` → "Iteration 15".

**Not done**: readiness-gate/competency wiring (next bottleneck candidate); Handover-doc path
(`POST /onboarding/handover-doc`) has code+tests but no runtime-verification yet either.

## Iterations 9-13 (2026-07-02, summarized)
Closed all leftovers from iteration 9 ("2 agents/2 tenants on 1 VM" isolation, tenant API-key
provisioning single-command flow + tests, `resolve_scope()` confirmed intentional not a bug).
Full detail preserved in `docs/product/PRODUCT_PROOF.md` and
`docs/operations/AUTONOMOUS_LOOP_LEDGER.md` (do not re-derive from conversation history).

## Iteration 14 (2026-07-02T21:15Z)
Fixed the last iteration-9 leftover: `tenant-replay-01` only had 1/1 host (`cust-edge`), no proof a
single tenant's Twin can merge facts from multiple distinct hosts. Installed a real second Remote
Agent for `tenant-replay-01` on VM `cust-app` (`/opt/omni-remote-agent-replay01/`, systemd unit
`omni-remote-agent-replay01.service`, reusing the existing gateway API key), alongside the
pre-existing `staging-sim` agent on that same VM. Runtime proof: agent log register/profile/evidence
all 200 OK; Redis `omni:aoip:system_model:tenant-replay-01` revision 54→66, facts now span
`{cust-edge, cust-app}` (was `{cust-edge}` only); `staging-sim` Twin on the same shared VM unaffected
(`{cust-edge, cust-db, cust-app}`/76 facts — isolation holds); `GET
/onboarding/competency?entity_type=host&entity_id=host:cust-app` with tenant-replay-01's bearer
token returns live VERIFIED facets. New
`tests/test_onboarding_pipeline.py::TestOneTenantTwoHosts` (2 tests). `pytest
tests/test_onboarding_pipeline.py -q` → 31 passed (was 29). Regression `-k "onboarding or
gateway_api or tenant or provision" --ignore=tests/integration` → 159 passed (was 157). Reconfirmed
`OMNI_AUTO_EXECUTE_ENABLED=false` (VM agent install, no K8s mutation). Full detail:
`docs/product/PRODUCT_PROOF.md` → "Iteration 14".

**Not done**: `cust-db` still has no agent for `tenant-replay-01` (2/3 host parity, not 3/3) — not a
bug, just unexplored scope. All iteration-9 leftovers are now closed. Next bottleneck TBD (candidates:
`cust-db` for full parity, or Phase 6/7 — UnderstandingComplete, Handover, operator portal UI).

## Deliverable
Slice "Repeatable Tenant Onboarding Baseline" (Continuous Productization Loop) via skill
`.claude/skills/omni-autonomous-productizer/`. Goal: prove a fresh lab tenant can go through
Tenant→Agent→Discovery→Fact→Twin→Competency→Unknown→Question→Claim without manual intervention.
Iterations 1-14 DONE (System Twin, multi-host Twin, gateway/aoip import fix, Fact provenance fix,
evidence compaction, canonical provisioning, tenant idempotency, second Agent + isolation proofs,
API-key provisioning). Iteration 15: closed the Human-Claim runtime-verification gap. Iteration 16:
closed the Handover-doc runtime-verification gap.

## Current state
- Branch `main` @ `5c6d225` (iteration 15 confirmed committed; iteration 16 is docs/runtime-proof
  only, staged for commit).
- Safety: `OMNI_AUTO_EXECUTE_ENABLED=false` unchanged; no K8s/VM mutation in iteration 16 (2 reads +
  one POST against gateway API using existing tenant tokens only). All core pods Running.
- Next step: pick one of iteration 15/16's remaining candidates — (a) design+wire
  `competency_matrix` coverage into the readiness/`UnderstandingComplete` gate (highest value,
  carried over twice now), (b) `cust-db` agent for `tenant-replay-01` (3/3 parity, lowest
  priority), (c) operator portal UI for competency/unknowns/diagram (API-only today). Full detail
  in `docs/operations/AUTONOMOUS_LOOP_STATE.json` → `iteration.next_step`.

## Note on unrelated commits
`8d4c0ed` and `7689049` (provider-portal / lab-incidents work) landed on `main` outside this skill's
tracking — confirmed unrelated to onboarding/Twin/Competency/tenant-replay-01, not a bug, just
parallel work merged to `main`. Not re-litigated each iteration; see git log for detail if needed.

## Working tree
This iteration updated `docs/product/PRODUCT_PROOF.md`, `docs/operations/AUTONOMOUS_LOOP_STATE.json`,
`docs/operations/AUTONOMOUS_LOOP_LEDGER.md`,
`.claude/skills/omni-autonomous-productizer/references/current-priority.md`, this file. No source
code or tests changed this iteration (runtime-verification only). Pre-existing unrelated
modifications in `docs/post-mortems/*.md` and `.autonomous-loop/` (supervisor runtime logs, not
source) are untouched by this iteration and not part of its commit.
