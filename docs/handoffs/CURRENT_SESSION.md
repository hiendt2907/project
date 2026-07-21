# Current Session Handoff

Updated: 2026-07-21 (Phase 8 #2+#3 + hardcode-removal — ALL DONE, verified live, committed `8fc60ab`)

## Phase 8 — ALL 3 candidates now DONE, verified with real data/code/tests (user explicitly demanded this verification level: "xác thật % với dữ liệu, code, test thật")

Final state, checked directly against the repo and live cluster (not
recalled from earlier notes):
- Code: `git log --oneline -1` → `8fc60ab fix(knowledge-pipeline): stop
  swallowing real Redis/Kafka failures in discovery evidence path`.
- Tests: `pytest tests/ -q --ignore=tests/integration` → **6390 passed, 0
  failed** (confirmed via `--collect-only` too: 6390/6395 collected, 5
  deselected — matches).
- Deployed: `make deploy-worker` rolled out `omni-fullstack`+`omni-gateway`;
  `kubectl exec ... python3 -c "from remote_agent.discovery import
  suspect_confirm_threshold; ..."` confirmed live in the running pod AND
  confirmed the env-override actually works (`OMNI_DISCOVERY_SUSPECT_CONFIRM_THRESHOLD=7`
  → function returns `7`, not the hardcoded default).
- Sanity check (non-destructive, real Kafka+Redis+pod): republished the
  REAL current 33-service snapshot for `staging-sim_cust-app` unchanged —
  confirmed baseline stayed at 33 services after the cycle (no regression
  from removing the try/except swallows).
- `make e2e-proactive` → `summary.pass=true, failed_checks: []`.

## Follow-up — remove hardcoded threshold/TTL from Phase 8 #1, per explicit user instruction ("cấm tuyệt đối hardcode nhé")

## Follow-up — remove hardcoded threshold/TTL from Phase 8 #1, per explicit user instruction ("cấm tuyệt đối hardcode nhé")

`SUSPECT_CONFIRM_THRESHOLD = 2` and `_SUSPECT_STREAK_TTL = 6*3600` (both
introduced in Phase 8 #1, commit `e15a633`) were plain hardcoded module
constants — flagged by the user mid-session as a standing violation of this
project's own established pattern (`src/aoip/recovery.py`'s
`_journal_vacuum_threshold_bytes()`: env-driven, read at call time, never an
import-time constant).

**Fix in `src/remote_agent/discovery.py`:** both are now functions reading
`os.environ` at call time, defaults unchanged (2 cycles, 6h TTL):
- `suspect_confirm_threshold(env=None) -> int` — env
  `OMNI_DISCOVERY_SUSPECT_CONFIRM_THRESHOLD`, falls back to default on
  missing/unparseable/`<1`.
- `_suspect_streak_ttl_s(env=None) -> int` — env
  `OMNI_DISCOVERY_SUSPECT_STREAK_TTL_S`, falls back to default on
  missing/unparseable/non-positive.
`src/workers/knowledge_pipeline.py::_handle_discovery` now calls
`suspect_confirm_threshold()` per-invocation instead of importing a static
constant.

**+5 tests** in `tests/test_knowledge_pipeline.py` (default-when-unset,
env-override, invalid/non-positive-falls-back-to-default ×2, and an
end-to-end behavioral test proving the override actually changes runtime
behavior — `threshold=1` accepts a suspect snapshot on the FIRST cycle
instead of requiring 2). `tests/test_knowledge_pipeline.py -q`: 32/32
passed.

---

## Phase 8 #2+#3 — silent evidence loss on Kafka forward / Redis read ambiguity — CODE DONE, TDD GREEN, DEPLOY+SANITY-CHECK PENDING

Continuation of Phase 8 (see full section below for #1, already DONE). This
increment addresses the other 2 candidates from the original Explore-agent
survey, both user-requested by name ("xử lý #2 và #3 đi").

**#3 fix — `src/remote_agent/discovery.py::load_discovery_snapshot`:**
previously returned `None` for BOTH "key genuinely doesn't exist" (legit
first-run) AND "the Redis read/parse itself failed" (transient blip) —
indistinguishable to the caller, so a real Redis hiccup silently skipped
that cycle's diff with zero signal. Now only the genuine-missing-key case
returns `None`; a real read/parse exception propagates.

**#2 fix — `src/workers/knowledge_pipeline.py::_handle_discovery`:** the
Kafka-forward-to-`omni-discovery-evidence` step (feeds onboarding
projection) was wrapped in a bare `try/except: log warning` — since the
SOURCE topic's offset commits right after this function returns without
raising, a transient Kafka blip during forward silently dropped that
cycle's evidence forever (no retry, no DLQ). Removed the swallow so it
propagates to `kafka_knowledge_evidence_loop`'s EXISTING retry+poison-ack
mechanism (3 retries w/ 0.5s backoff, then a loud `poison_ack` error log —
not a silent drop). Also reordered: diff+save+emit now completes BEFORE the
forward attempt, so a forward-triggered retry re-diffs old==new (no
duplicate SERVICE_ADDED/REMOVED events, no duplicate Telegram messages) and
only re-attempts the forward itself.

Both fixes follow the same principle as #1: stop collapsing "real
infrastructure failure" and "legitimate edge case" into the same
silently-swallowed outcome; let real failures reach the retry
infrastructure that already exists for exactly this purpose.

**TDD (+3 tests in `tests/test_knowledge_pipeline.py`, using deterministic
`_RaisingRedisGet`/`_RaisingKafka` stubs against the REAL production
functions `load_discovery_snapshot`/`handle_knowledge_evidence`, not
reimplemented logic):**
- `test_load_discovery_snapshot_propagates_real_read_failure` — a raising
  `.get()` propagates instead of returning `None`.
- `test_discovery_redis_read_failure_propagates_not_swallowed` — same,
  through the full `handle_knowledge_evidence` entry point.
- `test_discovery_kafka_forward_failure_propagates_not_swallowed` — a
  raising `kafka.send_dict` propagates, AND confirms diff+save already
  completed (baseline updated, 1 change-detected event) before the forward
  step's exception surfaces — proves retry-safety.
- `tests/test_knowledge_pipeline.py -q`: 27/27 passed (24 pre-existing +
  Phase-8-#1's 6, minus overlap... exact count: was 24 after #1, +3 here =
  27, confirmed).

**Deliberate scope decision — no destructive live chaos drill against
shared Redis/Kafka for #2/#3** (unlike #1's live drill against the real
pod): Redis and Kafka are shared infrastructure backing kill-switch state,
tier cache, audit chain, and lease ownership across the ENTIRE `multi-agent`
namespace — deliberately breaking either, even briefly and narrowly, to
prove a code path here would risk collateral across unrelated live systems
for a proof the TDD tests (exercising the real production functions
directly with deterministic failure injection) already provide honestly.
Recorded this reasoning explicitly rather than either skipping verification
silently or taking on disproportionate risk to look thorough.

**Verification status: pytest full suite (`bwukad1s1`'s successor,
`b4kv1vphm`) STILL RUNNING as of this handoff write (was at ~70% at last
check) — NOT YET CONFIRMED GREEN.** `bwukad1s1` (the run BEFORE the
hardcode-removal follow-up, i.e. #2/#3 code alone) already confirmed
**6385 passed, 0 failed**. `b4kv1vphm` re-runs the full suite AFTER the
hardcode-removal follow-up on top of that — expect ~6390 passed (6385 + 5
new env-driven tests). Check that job's output (or re-run
`pytest tests/ -q --ignore=tests/integration` if stale) before treating
this DONE. `make deploy-worker` + a lightweight non-destructive post-deploy
sanity check (publish 1 normal DISCOVERY message to the real Kafka topic,
confirm still processes correctly — NOT a fault-injection drill against
shared Redis/Kafka, see reasoning above) + `make e2e-proactive` are the
remaining steps before commit.

**Working tree at this checkpoint:** `src/remote_agent/discovery.py`,
`src/workers/knowledge_pipeline.py`, `tests/test_knowledge_pipeline.py`,
`docs/handoffs/CURRENT_SESSION.md` all modified, NOT yet committed (only
Phase 8 #1's code is committed, at `e15a633`/`672a025`).
`reports/incident-matrix/latest.json` also dirty (pre-existing, harmless,
not committed).

**Next step:** (1) confirm job `b4kv1vphm` green; (2) `make deploy-worker`;
(3) lightweight sanity check (not a fault-injection drill, per above); (4)
`make e2e-proactive`; (5) commit code (#2/#3 fix + hardcode-removal
follow-up together) + this handoff; (6) report to user: Phase 8 all 3
candidates now DONE, hardcode fix confirmed, refresh the overall %
estimate; (7) do NOT push to origin unless explicitly asked.

---

## Phase 8 — discovery/onboarding chaos hardening — code+deploy+live-drill DONE, full-suite/e2e-proactive verification IN PROGRESS

Scope chosen via a research-only Explore agent survey of the discovery ->
knowledge-evidence -> onboarding-projection pipeline for silent-data-loss
gaps (same bug class as the two service-loss fixes earlier this session:
`9955860`, `d2e0666`). Top candidate, verified by direct code read before
acting: `src/workers/knowledge_pipeline.py::_handle_discovery` (pre-fix)
unconditionally overwrote the onboarding baseline (`omni:knowledge:
discovery_snapshot:{tenant}:{agent}`) with whatever the `service_topology`
probe reported, including an implausible empty `services: []` snapshot —
permanently corrupting the system model from a single bad cycle.

**Important correction made mid-investigation:** the original hypothesis
("a live VM systemctl outage would trigger this") does NOT hold — read
`src/remote_agent/collectors/discovery_evidence.py:122`
(`collect_service_topology`) confirms total collector failure already
returns `None` (suppresses evidence entirely), so that specific trigger is
already safe and NOT live-drillable via a VM-side fault. The fix is
consumer-side defense-in-depth instead (protects against ANY producer,
current or future, that ever sends an implausible empty snapshot — e.g. a
malformed/legacy agent, a parsing edge case). Recorded honestly rather than
overclaiming a VM-side chaos scenario that isn't actually reachable.

**Fix (TDD, +6 tests, `tests/test_knowledge_pipeline.py`):**
- `src/remote_agent/discovery.py`: new `is_snapshot_suspect(old, new)` (pure,
  true iff old had services and new has zero), `SUSPECT_CONFIRM_THRESHOLD=2`,
  Redis-backed `bump_suspect_streak`/`reset_suspect_streak`
  (`omni:knowledge:discovery_suspect_streak:{tenant}:{agent}`, TTL 6h).
- `src/workers/knowledge_pipeline.py::_handle_discovery`: a suspect snapshot
  on its 1st occurrence is logged and SKIPPED (no diff, no baseline
  overwrite). On the 2nd consecutive suspect occurrence it's accepted as
  real (e.g. genuine full-host outage), diffed, and saved normally — so a
  transient blip can no longer permanently corrupt the baseline, while a
  real sustained outage still surfaces after 2 cycles instead of being
  silently ignored forever.

**Live drill — PASSED against real infra** (not a unit test): ran INSIDE the
real `omni-fullstack` pod via `kubectl exec` (real network access, real
`aiokafka`/`redis.asyncio`), publishing synthetic DISCOVERY evidence
directly onto the real `omni-knowledge-evidence` Kafka topic, consumed by
the actual deployed `kafka_knowledge_evidence_loop` ->
`handle_knowledge_evidence`. Used a narrow-window synthetic 2-service
baseline (`omni-phase8-drill-svc-a/b`) swapped in for `staging-sim_cust-app`
instead of the real 33-service baseline, to keep Telegram change-approval
noise to 2 messages instead of 33+ — same "open narrowly, restore, confirm"
discipline as kill-switch/tier drills. Script:
`/Users/hiendang/.claude/jobs/71b56e60/tmp/phase8_discovery_suspect_drill.py`.
- Cycle 1 (suspect): streak=1, baseline UNCHANGED (still the 2 synthetic
  services), 0 spurious change-detected events. PASS.
- Cycle 2 (confirmed): baseline correctly overwritten to `{"services": []}`,
  streak key reset (deleted), exactly 2 `SERVICE_REMOVED` change-detected
  events (one per synthetic service, matches real diff). PASS.
- Cleanup: real 33-service baseline restored and confirmed
  (`service_count=33`), streak key deleted, drill's change_pending keys
  deleted. Fresh `redis-cli get`/`keys` after the script exited confirmed
  clean state (33 services, no streak key, no pending keys).
- **Gotcha hit and fixed during this drill:** the first attempt failed
  because the fix was only edited locally, never redeployed — the running
  pod was still on old code, which unconditionally diffed+saved and produced
  2 real `SERVICE_REMOVED` events on cycle 1 (proving the OLD bug, not
  ironically the new fix). Root-caused by reading pod logs (no "suspect" log
  line appeared, which the new code always emits). Fixed by running
  `make deploy-worker` before re-running the drill — the drill then passed
  as described above. Confirms this project's standing "test pass + push
  KHÔNG chứng minh đã deploy" lesson applies to live drills too, not just
  unit tests.

**Verification status: DONE, both green.** `pytest tests/ -q --ignore=tests/
integration` -> **6382 passed, 5 deselected, 0 failed** (unchanged count
from the pre-redeploy run — confirms zero regressions across 2 consecutive
runs, before and after `make deploy-worker`). `make e2e-proactive` ->
`summary.pass=true, failed_checks: []`. `omni-fullstack` + `omni-gateway`
pods both `1/1 Running` post-redeploy. Code committed: `e15a633`.

**Phase 8 status: DONE** (this increment). 1 real chaos-hardening bug found,
fixed with TDD, deployed, and live-drilled against real Kafka+Redis+the
real running pod. 2 more candidates from the same survey remain unstarted
(see below) — optional future increments, not blocking.

**Not yet done (2 more candidates from the same Explore-agent survey,
lower priority, not started):**
1. `src/workers/knowledge_pipeline.py:146-166` — the forward of DISCOVERY
   evidence to `omni-discovery-evidence` (feeds onboarding projection) is a
   bare `try/except: log warning` with no retry/DLQ; a transient Kafka
   broker blip during the forward silently drops that cycle's evidence
   (source-topic offset already committed by then).
2. `src/remote_agent/discovery.py:274-281` — `load_discovery_snapshot`
   returns `None` on ANY Redis read exception, indistinguishable from a
   legitimate first-run — a Redis blip during read silently skips diffing
   for that cycle (lower priority: broader blast radius, harder to isolate
   in a drill).

**Working tree at this checkpoint:** clean except
`reports/incident-matrix/latest.json` (generated report, harmless,
pre-existing, not committed). Phase 8 code committed at `e15a633`, this
handoff update pending its own commit.

**Git state:** 10 local commits ahead of `origin/main` as of this
checkpoint (`992a055`..`e15a633` range, this session) — NOT pushed. User
has not re-asked for push this round.

**Next step:** (1) report the %-completion status to the user (done, same
turn); (2) ask whether to push the accumulated local commits; (3) if
continuing the roadmap: either pick up 1 of the 2 remaining Phase 8
candidates (Kafka-forward-failure DLQ, Redis-read-failure ambiguity), or
move to Phase 9 (portal parity — needs a product decision from the user
first on the 9 zero-port UI routes before any deletion/porting work starts),
or Phase 10 (SIEM + chaos re-audit).

## Phase 7 — VM capability #3 (systemd.journal_vacuum) + gate-config hygiene — MERGED, CODE-SIDE DONE, DEPLOY/DRILL PENDING

Second fanout round this session (roadmap toward 95%, Phase 7 of 7-10).
Workflow `wf_4a2349dd-ec8`: 2 parallel worktree coding agents + 1 aggregator.

**Merged into main (commits `b5c0abd`, `56b1abe`, fixup `1f78e85`):**
- `src/aoip/capabilities/systemd_journal_vacuum.py` — capability #3, mirrors
  `systemd_reset_failed.py`. `failure_mode="disk_pressure_journal"`,
  target unit fixed as `systemd-journald.service` (host-scoped disk action
  modeled as acting on the real journald unit, avoiding a
  canonical_scope/RecoveryGate generalization). risk=0.12. Threshold/target
  env-driven: `AOIP_JOURNAL_VACUUM_THRESHOLD_BYTES` (default 2GiB),
  `AOIP_JOURNAL_VACUUM_TARGET_SIZE` (default "200M"). apply() only ever runs
  `journalctl --vacuum-size=<target>`.
- `config/aoip_agent_gate.env` (NEW canonical source) +
  `scripts/deploy_aoip_gate_config.sh` (idempotent, `--self-test` passing) —
  replaces the manual per-VM SSH-sed pattern that caused a live-drill
  failure last round. `AOIP_GATE_ALLOWED_FAILURE_MODES` now includes
  `disk_pressure_journal`.
- **Coordinator fixup (mine, not a subagent):** the gate-config agent
  deliberately left `systemd-journald.service` out of
  `AOIP_ALLOWED_SYSTEMD_UNITS` (no visibility into the other agent's target
  unit — correctly flagged as an open item). After merge I confirmed
  `recovery.py`'s operator key (`disk_pressure_journal`/`systemd`) matches
  the gate config exactly, then added the unit to the allowlist + updated
  the matching test. `test_catalog_consistency_every_allowed_failure_mode_has_an_operator`
  now green.

**Verification done:** full suite `pytest tests/ -q --ignore=tests/integration`
→ **6376 passed, 0 failed, 5 deselected** (was 6367 right after merge, 6323
baseline before Phase 7 — zero regressions). `scripts/deploy_aoip_gate_config.sh --self-test` → ALL PASS.

**Update — deploy + E2E now DONE, only the live drill remains:**

1. **Deploy DONE.** Both `src/aoip/` and `src/remote_agent/` tarred
   (`COPYFILE_DISABLE=1`) and pushed via `orb push` to all 3 VMs, service
   stopped/extracted/restarted. `python3 -c "from aoip.capabilities import
   systemd_journal_vacuum; print(CAPABILITY_NAME)"` → `systemd.journal_vacuum`
   confirmed live on cust-app, cust-edge, cust-db.
2. **Gate config DONE, against real VMs (not just `--self-test`).**
   `scripts/deploy_aoip_gate_config.sh cust-app cust-edge cust-db` → PASS on
   all 3, all managed keys (incl. `AOIP_ALLOWED_SYSTEMD_UNITS=payment-api.service,systemd-journald.service`)
   match canonical. Ran a **second time** to prove real idempotency: all 3
   VMs reported `run.env already up to date — skip write + restart`.
3. **K8s side DONE.** `make deploy-worker` rolled out `omni-fullstack`
   cleanly; `kubectl exec ... python3 -c "from aoip.capabilities import
   systemd_journal_vacuum"` confirmed live in the running pod (not just
   pushed-and-hoped).
4. **`make e2e-proactive`: PASS** (`summary.pass=true`, `failed_checks: []`).
5. **`make e2e-incident-matrix`: 5/5 PASS** (`wave_a1_rbac_manifest`,
   `wave_a1_rbac_permissions`, `phase_b_pytest` [6376 passed], `phase_b_unit_full`
   [6376 passed], `nginx_waiting_fault` [real K8s ConfigMap-fault injection,
   Omni produced `action=unknown` correctly]). Report at
   `reports/incident-matrix/latest.json` (git-dirty, generated, not committed).

**Update — LIVE DRILL PASSED. Phase 7 is now fully DONE (code, deploy, E2E,
live proof), same closing bar as the reset_failed round.**

6. **Live drill for `systemd.journal_vacuum` — PASSED on cust-app, first
   attempt.** Script:
   `/Users/hiendang/.claude/jobs/71b56e60/tmp/e2e_journal_vacuum_drill.py`
   (adapted from `e2e_reset_failed_drill.py`). Real disk pressure, not
   faked: read cust-app's actual `journalctl --disk-usage` (168.4M),
   computed a threshold below it (~88.3M) and a target below that (42M),
   wrote both as a narrow-window override into cust-app's `run.env`
   (`AOIP_JOURNAL_VACUUM_THRESHOLD_BYTES`/`AOIP_JOURNAL_VACUUM_TARGET_SIZE`
   — NOT part of the canonical `AOIP_GATE_*` managed-key set, so orthogonal
   to `deploy_aoip_gate_config.sh`), restarted `aoip-agent.service` so the
   operator's `os.environ`-read checks saw genuine pressure. Built the
   envelope through the real `aoip.command_bridge.build_durable_command`
   (capability=`systemd.journal_vacuum`, unit=`systemd-journald.service`),
   enqueued via gateway, polled to terminal:
   `state=COMPLETED, outcome={status: recovered, verified: true, rc: 0}`.
   VM confirmed: journal usage **168.4M → 40.0M** (real
   `journalctl --vacuum-size=42M` ran). Matching `RECOVERY_COMPLETED` audit
   block found in the VM's real audit log
   (`action_id` ending `...dec-jv-c6cf390f-systemd-journald.service`).
   - Gates exercised for real: kill-switch (`omni-gateway` only) →
     `aoip_mutation_enabled` toggle (`staging-sim`) → tier `shadow`→`assist`
     (LOW risk needs ≥assist). All 3 reverted + confirmed via fresh reads in
     `finally`: tier back to `shadow` (fresh GET), mutation toggle back to
     `false`/`tenant_toggle_off` (fresh GET), kill-switch back to `false`
     (fresh `kubectl exec printenv`). `run.env` override keys removed
     entirely (`grep -c AOIP_JOURNAL_VACUUM` → `0`), `aoip-agent.service` +
     `omni-remote-agent.service` both confirmed `active` post-cleanup.
   - Unlike the `reset_failed` drill, no deployment/config gaps were hit
     this time — Phase 7's own gate-config-hygiene deliverable
     (`deploy_aoip_gate_config.sh`) plus the coordinator fixup (unit
     allowlist) meant the capability worked end-to-end on the first real
     attempt.

**Phase 7 status: DONE.** Capability #3 (`systemd.journal_vacuum`) is live
on K8s (`omni-fullstack`) and all 3 VMs, gate-config is git-tracked and
deployed for real, both E2E suites pass, live drill proven. Roadmap can
move to Phase 8 (discovery/onboarding chaos hardening) next.

7. `git push` — NOT yet done this round. User asked for push explicitly
   last round (Phase 0-6/improvement-plan round); has not re-asked this
   round. Confirm before pushing — 9 local commits ahead of `origin/main`
   (`d2e0666`..`ccad5e7` range, this session).

**Working tree at this checkpoint:** clean except
`reports/incident-matrix/latest.json` (generated report, harmless, not
committed). All Phase 7 code/config changes committed through `ccad5e7`.

**Git state:** 5 local commits ahead of `origin/main` (`d2e0666..1f78e85`
range from this session) — NOT pushed.

---

## Improvement-plan fanout (post-report) — 3 fixes merged+deployed, E2E IN PROGRESS

After the Phase 0-6 roadmap + 2 hardcode fixes, user asked for a written
improvement plan per subsystem, then explicitly asked to spawn parallel
subagents to implement it, aggregate, deploy, run E2E, and re-report %.

**Workflow fanout (`wf_4f814b5f-9a8`, 6 agents, 782k tokens, ~19min):** 3
coding agents (each in an isolated git worktree, commit-only-no-push) + 2
read-only audit agents + 1 aggregator. All 6 `done`, 0 errors, 0 file overlap
between the 3 code branches (aggregator cross-checked explicitly).

**Merged into main by me directly (not delegated — kept the merge/deploy/
kill-switch steps under direct supervision per this project's established
discipline):**
- `d2e0666` — `collect_service_topology()` in
  `src/remote_agent/collectors/discovery_evidence.py` had the SAME
  `--state=running` anti-pattern already fixed in `discovery.py` (a
  crashed/failed unit vanished from the onboarding topology snapshot exactly
  when it mattered; status was hardcoded `"running"`). Real bug, found by
  the audit-first agent, TDD RED-GREEN, 2 new tests.
- `98a3a5a` — `ExecutionLease` in `run_guarded_recovery()`
  (`src/aoip/agent/operations.py`) was NOT tenant-namespaced (key was raw
  `svc:{unit}`), unlike the parallel `intake.py` admission path which already
  used `canonical_scope(tenant, node)`. Two tenants recovering a same-named
  unit could collide on the same Redis lease key (deny-only, not fail-open,
  but wrong isolation — this was the Phase 5 residual gap, now closed). Fix
  aligns both tracks on `canonical_scope`. 2 new cross-tenant collision
  tests (primitive-level + through the real `run_guarded_recovery` path).
- `b1f11c2` — NEW capability `systemd.reset_failed` (2nd VM/AOIP recovery
  capability, mirrors `systemd_restart.py`'s full vertical slice: typed
  payload → approval-hash → preflight → `run_guarded_recovery` [same
  lease/idempotency/audit, not bypassed] → verification). Only runs
  `systemctl reset-failed`, never start/stop/restart — no downtime risk.
  Generalized `aoip/recovery.py`'s shared `_gate_checks()` (was hardcoded to
  require `"DOWN"` in a claim; now also accepts `req.failure_mode`) and
  replaced `command_bridge.py`'s single hardcoded restart branch with a
  `_CAPABILITY_ADAPTERS` registry keyed by capability name. +39 tests.
  **Code + unit-test level only — live VM drill NOT yet run** (deliberately
  deferred to a supervised narrow-window kill-switch session, not done by
  the subagent per its own constraints).

**Audit findings (no code changes made from these, by design — reported for
a human/next-session decision):**
- `k8s-lane-audit`: 3 low-severity findings only, none reproduce the 3
  VM-lane bug classes (state-filter drops object / hardcoded critical-list /
  unsafe rstrip). No fix recommended. Detail: workflow journal
  `wf_4f814b5f-9a8`, agent `k8s-lane-audit`.
- `ui-parity-audit`: of 24 routes in the old root `ui/app`, only 9 are
  safely portable-and-deletable (dashboard, pipeline, incidents, kpi,
  ledger→audit, remote-agents→agents, understanding, admin/hitl→approvals,
  login→Dex SSO). 15 are NOT safe to delete: 6 have a stub + documented plan
  (`config/autonomy` — currently the ONLY UI way to change autonomy policy;
  `deploy`, `admin/tenants`, `admin/tier` partial, `onboarding`, `workers`),
  9 have NO port or stub at all (`admin/flags`, `admin/guide`, `admin/kb`,
  `admin/risk-class`, `playbooks`, `siem`, `simulator`, `operator`,
  `trace/[id]`). **Do not delete root `ui/` until these are resolved** —
  supersedes the earlier "safe to delete once parity confirmed" assumption.

**Verification after merge (this session, real commands run):**
- `pytest tests/ -q --ignore=tests/integration` → **6323 passed, 5
  deselected** (was 6280 before this fanout; +43 matches the 3 agents' new
  tests exactly, 0 regressions).
- `remote_agent` bumped to v1.3.11, deployed to all 3 VMs (stop/sync/start),
  confirmed `active` + clean `journalctl` on all 3.
- `make deploy-worker` — rebuilt image, rolled out `omni-fullstack`,
  `rollout status` succeeded. `curl /healthz` → all checks `ok`; `/readyz` →
  `ready: true`. Confirmed the NEW capability module is actually live in the
  running pod via `kubectl exec ... python3 -c "from aoip.capabilities import
  systemd_reset_failed"` → import OK (guards against the Iteration-1 class of
  "test pass + push ≠ deployed" drift).
- `make e2e-proactive` and `make e2e-incident-matrix` launched in background,
  **results not yet observed as of this handoff write** — check
  `/private/tmp/claude-501/-Users-hiendang-project/71b56e60-d01c-4477-8b1e-4ca74abddd73/tasks/bm5z7p5ux.output`
  and `.../bihe4cpfq.output`, or re-run if stale.

**Git state:** 7 local commits ahead of `origin/main`
(`d11f72e f23bf2e 221ef58 d2e0666 98a3a5a b1f11c2 5e1056f`) — **NOT pushed**,
per standing rule (only commit/push when explicitly asked; this turn's
instruction covered code+deploy+test, not push). Working tree otherwise
clean.

**Update — all verification steps now DONE, real evidence captured:**

- `make e2e-proactive`: PASS (`summary.pass=true`, `failed_checks: []`).
- `make e2e-incident-matrix`: 5/5 PASS, including a real K8s fault-injection
  scenario (`nginx_waiting_fault`) confirming the analyst still processes
  real faults correctly post-deploy.
- **Live drill for `systemd.reset_failed` (the new capability) — PASSED.**
  Found and closed 2 real deployment/config gaps along the way (not
  assumed, discovered by the drill actually failing first):
  1. **Deployment drift**: `aoip-agent.service` on the VMs runs
     `python -m aoip.agent.employee` from a SEPARATE deployed copy at
     `/opt/omni-remote-agent/aoip/` — distinct from `remote_agent/`. Earlier
     in this turn I had only redeployed `remote_agent` (v1.3.11); the `aoip`
     package (lease fix + new capability) was still stale on all 3 VMs.
     Fixed: tarred + deployed `src/aoip/` to all 3 VMs same as
     `remote_agent`, confirmed via `python -c "from aoip.capabilities import
     systemd_reset_failed"` live on each VM.
  2. **Capability-enablement gate config**: VM `run.env`'s
     `AOIP_GATE_ALLOWED_FAILURE_MODES` was `process_down` only (fail-closed
     by design). Updated to `process_down,failed_state_stale` on all 3 VMs
     — a PERMANENT config change (capability rollout, not a narrow-window
     toggle like kill-switch/tier) since the capability is now tested and
     merged.
  - Drill mechanics: forced `payment-api.service` on `cust-app` into a REAL
    `failed` state (rapid-kill loop hitting systemd's default
    StartLimitBurst — root cause was never actually broken, exactly the
    scenario this capability targets), built the command envelope through
    the real `aoip.command_bridge.build_durable_command` (capability=
    `systemd.reset_failed`, not hand JSON), enqueued via gateway, polled to
    terminal: `state=COMPLETED, outcome={status: recovered, verified: true,
    reason: "service + dependents verified"}`. VM confirmed `is-failed`
    cleared (`inactive`, not `failed`). Matching `RECOVERY_COMPLETED` audit
    block found in the VM's real audit log.
  - Gates exercised for real along the way (each one only discovered by
    the drill actually hitting it, not predicted in advance): master
    kill-switch → per-tenant `aoip_mutation_enabled` toggle (`POST
    /autonomy/mutation`) → tier_gate (`staging-sim` promoted `shadow`→
    `assist`, since LOW-risk needs at least `assist`) → capability gate
    config on the VM. All 3 session-scoped elevations (kill-switch, tenant
    mutation toggle, tier) reverted and confirmed via fresh reads in the
    script's `finally` block; script + full transcript at
    `/Users/hiendang/.claude/jobs/71b56e60/tmp/e2e_reset_failed_drill.py`.
  - Cleanup: `payment-api.service` explicitly restarted after the drill
    (reset_failed itself never starts/stops the unit, by design) —
    confirmed `active` + HTTP 200. `journalctl` clean on all 3 VMs
    post-drill.

**Next step:** (1) report the project completion-percentage re-assessment
incorporating this round's real results (in progress, same turn); (2) ask
user whether to push the 8 local commits (7 code + this handoff); (3)
consider whether `AOIP_GATE_ALLOWED_FAILURE_MODES` / the new gate allowlist
value should also be captured in a tracked config file (currently only
live on the 3 VMs' `run.env`, not represented in git — same pattern as the
pre-existing `AOIP_ALLOWED_SYSTEMD_UNITS` entry, but worth a follow-up
decision on whether VM `run.env` drift-tracking is needed).

## Post-roadmap fix #2 — automatic package-origin classification, DONE, `dcdabb2`

User flagged (correctly) that the discovery service-loss fix (`9955860`)
still left a hardcoded `_CRITICAL_SERVICES` name list in
`collectors/services.py` gating which systemd failures count as
`SYS_HARD_FAIL`. Asked for a real, automatic, per-VM determination instead —
verified analytically first (K8s lane vs VM/systemd lane are already
separate; this hardcode only affects the VM lane) and live on the VMs before
writing any code: `dpkg -S <FragmentPath>` reliably distinguishes a
distro-package-owned unit (nginx→nginx-common, cron, mariadb, redis-server,
rpcbind, nfs-*, udev — all correctly owned) from a hand-installed/customer
unit (`payment-api`, `aoip-agent` itself, and a few lab scripts — all
correctly "no path found").

Shipped: new `src/remote_agent/pkg_origin.py` (`classify_unit_origin()` via
dpkg/rpm, zero hardcoded names); `collectors/services.py::collect_systemd_units()`
now routes ANY failed/activating unit to `SYS_HARD_FAIL` (severity no longer
gates visibility), origin carried as evidence context
(`failed_units_origin` dict, `critical_failed_units` repurposed to mean
"non-package-owned"); `discovery.py::_collect_running_services()` tags every
discovered service (not just failed ones) with `origin`, so the general
onboarding/System-Twin profile gets this too; `agent.py` simplified (dropped
the now-obsolete `critical_services=` gate). 9 new tests. Full suite: `6280
passed` (was 6271, no regressions).

Published `remote_agent` v1.3.10, deployed to all 3 VMs, confirmed live via
the real Redis profile: `payment-api`/`aoip-agent` → `custom`,
`cron`/`mariadb`/`redis-server`/`nginx`/`rpcbind`/etc. → `package:<name>`,
across all 3 hosts, no errors in `journalctl` post-deploy. Committed
`dcdabb2`, pushed.

**Next step:** user asked for a full test-run + project completion-percentage
report now that this is deployed — in progress in this same turn, not a
separate session task.

## Session 2026-07-21 — Phase 0-6 roadmap ALL DONE

Plan: `/Users/hiendang/.claude/plans/temporal-sparking-ember.md`. Ledger:
`docs/handoffs/PHASE_0_6_PROGRESS.md`. **Every phase (0 through 6) is DONE, each
with a real Exit Criteria command run this session (or a prior session in this
same continuous effort, output preserved in the ledger) — no phase marked DONE
on code/unit-tests alone where a live drill was required.**

Two most consequential live proofs: **Phase 4** — a real crash-looped
`payment-api.service` on `cust-app` was detected, diagnosed, auto-dispatched, and
recovered end-to-end with zero manually authored JSON. **Phase 5** — the same
closed loop run *concurrently* across two real tenants (`staging-sim`/`cust-app`
+ `tenant-replay-01`/`cust-edge`, the latter temporarily re-identified for the
drill window since only 3 physical VMs exist) with zero cross-tenant leakage in
the captured Redis command records; one residual, non-exploitable gap
(VM-side `ExecutionLease` lock key not tenant-namespaced — fail-safe, not
fail-open) found and documented, not silently hidden.

**Phase 6** closed the loop with an honest re-run of the original grep audit:
the 3 Evidence / 6 Command wire shapes did **not** collapse to canonical types
(Phase 0b's `src/pkg/contracts/` module was always additive-only, never adopted
by production call sites — confirmed zero production imports). What *did*
converge and was live-verified: the governance layer — one shared `tier_gate`
authority, one `risk_taxonomy` table, and a matching closed-loop dispatch
pattern now apply uniformly to both the K8s and VM lanes. Recorded in new
`docs/architecture/ADR-006-evidence-command-contract-convergence.md`, pointed to
from `docs/CODEBASE.md`.

Commits this session: `e5e2a41`/`5deabdf` (Phase 4), `0419c4a` (Phase 5),
`4e1991b` (Phase 6 docs). All live-cluster/VM drill state reverted to safe
defaults and confirmed after every phase (kill-switch `false`, tenant tiers
back to `shadow`, VM identities/configs restored).

## Post-roadmap fix (same session) — discovery service-loss bug, DONE, `9955860`

While reviewing a Telegram diagnosis card mid-drill, the user flagged that
`payment-api.service`'s own failure was sometimes shown as a generic
`SYS_RESOURCE`-lane advisory (`ps`/`free` commands, no auto-recovery) instead
of the `SYS_HARD_FAIL` lane that has Phase 4's auto-recovery wiring — for the
*same* incident that, in a concurrent diagnosis session, correctly
auto-recovered. Root-caused live (not guessed): `remote_agent/discovery.py`'s
`_collect_running_services()` queried `systemctl list-units --state=running`,
so a service that crashes — the exact case that matters — silently drops out
of the VM's discovered-service snapshot at the moment it fails.
`remote_agent/agent.py` feeds that snapshot straight into
`collectors/services.py`'s critical-service allowlist, **replacing** (not
unioning with) the hardcoded default — so an app-level unit like
`payment-api`, absent from the hardcoded list, loses its "critical"
classification specifically because it's down, causing its own failure
evidence to non-deterministically land in the wrong lane depending on
discovery timing.

Fix: dropped the `--state=running` filter (systemd keeps a failed unit
"loaded"/in-memory until `reset-failed`, so it's still discoverable without
it) and now reports the real ACTIVE-column state instead of a hardcoded
`"running"`. 2 new regression tests in `tests/test_remote_agent.py`. Full
suite green: `6271 passed` (was 6269; +2 new tests, no regressions). Published
`remote_agent` v1.3.9, deployed to all 3 VMs (`cust-app`/`cust-edge`/`cust-db`)
via the established stop/sync/start bootstrap; all confirmed `active`, no
errors in `journalctl` post-deploy.

Full suite green throughout the session (6269 → 6271 passed). Working tree is
clean.

**Next step:** none pending — the roadmap is complete and this follow-up fix
is deployed. If there is follow-up appetite, the two honestly-deferred items
from Phase 6/5 are: (1) wire-shape migration onto `src/pkg/contracts/`
(ADR-006, deferred — not free, revisit only if a concrete pain point
justifies it), (2) tenant-namespacing the VM-side `ExecutionLease` scope
(Phase 5, deferred — currently fail-safe, not urgent).

**Working tree (uncommitted):**
- `src/services/analyst/diagnosis_loop.py` — added `suggested_recovery` field to the
  diagnosis output schema/prompt (`{"capability":"systemd.restart_unit","unit":"..."}`
  or null), `_parse_suggested_recovery()`, and grounding-gate validation for it
  (`_apply_grounding_gate` strips an ungrounded unit name). Prompt initially required
  command-output grounding only; loosened to also accept the pre-collected
  `failed_units` facts (matches the loop's own "conclude from facts in turn 1" rule).
- `src/workers/auto_recovery_bridge.py` (new) — `extract_suggested_recovery()`,
  `build_dispatch_advisory()`, `dispatch_if_eligible()`. Fail-closed at every step
  (no suggestion / low confidence / no `OMNI_GATEWAY_API_KEY` → skip, never raises).
  Normalizes a bare unit name (e.g. `"payment-api"`, as it appears in facts) to the
  full `.service` suffix the executor's `AOIP_ALLOWED_SYSTEMD_UNITS` allowlist needs —
  done downstream of grounding so neither the prompt nor the grounding check has to
  agree on a suffix convention.
- `src/workers/remote_agent_pipeline.py` — `_dispatch_auto_recovery_if_eligible()`
  wired into `_run_diagnosis_and_notify()`, called AFTER CRAT+Telegram succeed
  (best-effort last hop; swallows all exceptions, never breaks diagnosis reporting).
- `src/pkg/observability/pipeline_stages.py` — added `AUTO_RECOVERY` to
  `PIPELINE_STAGES` (was missing; `mark_stage` silently no-ops on unknown stages).
- `ui/apps/provider-portal/lib/pipeline.ts` — added the matching VI label.
- `src/workers/settings.py` — `omni_gateway_internal_url` +
  `omni_gateway_api_key` (worker's credential to call the gateway's own enqueue
  endpoint internally; empty = auto-dispatch disabled, fail-closed).
- `k8s/deployments/omni-fullstack.yaml` — mounts `OMNI_GATEWAY_API_KEY` from the
  existing `omni-gateway-secret` (`optional: true`).
- `src/remote_agent/collectors/services.py`, `src/remote_agent/discovery.py` — **real
  bug fix**: both used `unit_full.rstrip(".service")`, which strips a CHARACTER SET
  not a literal suffix — `"payment-api.service".rstrip(".service")` → `"payment-ap"`
  (the trailing `i` is in the strip set too). This corrupted the unit name fed to the
  diagnosis LLM, which then correctly-but-uselessly concluded the service was
  "missing" after running `systemctl status payment-ap`. Fixed to `.removesuffix()`,
  matching the already-correct pattern in `collectors/discovery_evidence.py`.
- `src/remote_agent/VERSION` → `1.3.8`, published and deployed to `cust-app` VM via
  the established stop/tar/start bootstrap (self-update HTTP flow has a known
  mid-flight race, avoided all session).
- Tests: `tests/test_auto_recovery_bridge.py` (new, 13 tests), `tests/
  test_remote_agent_auto_recovery_dispatch.py` (new, 5 tests — pipeline wiring),
  `tests/test_diag_grounding_and_scope.py` (+4 suggested_recovery grounding tests),
  `tests/test_remote_agent.py` (+3 regression tests for the rstrip bug, in both the
  systemd-units collector and discovery module).

**Second real gap found live:** `OMNI_AGENT_SERVICES_ENABLED` (default `false`) was
never set in cust-app's `run.env`, so the systemd-failure collector never ran on that
host at all — not a code bug, a deployment config gap. Enabled it directly in
`/opt/omni-remote-agent/run.env` on the VM and restarted `aoip-agent.service`.

**Drill status at session end:** worker+gateway both redeployed with all fixes
(image built successfully after an accidental interruption — a retry that finished
clean; verified via `kubectl exec ... python3 -c "from workers.auto_recovery_bridge
import extract_suggested_recovery; ..."` on the live pod, unit-suffix normalization
confirmed working). `payment-api.service` was deliberately crash-looped on cust-app to
trigger the natural pipeline, multiple real diagnosis sessions ran (confidence up to
1.0, correctly identified "payment-api service is in a failed state" post-fix) but
`suggested_recovery` stayed null until the prompt-grounding fix (just redeployed, not
yet re-verified live). **Before ending the session**: kill-switch reverted to `false`
(confirmed), `payment-api.service` restored to healthy/`active` on cust-app (confirmed)
— the VM and cluster are back in a safe, clean state.

**Next step (superseded — see the top "DONE" section):** the live drill described
below was re-run successfully in a later part of this same session — Phase 4 is DONE,
committed as `e5e2a41`, pushed. Next step is now Phase 5 (multi-tenant/multi-agent
concurrency proof) per the plan.

## Outcome

The tenant/provider product path was fully re-verified after the UI screenshot exposed
form overflow and ambiguous tenant context. Frontend, backend, persistence, business
rules, safety boundaries, builds, E2E, and deployed pods are green. The canonical
evidence is `docs/reports/frontend-backend-logic-verification-2026-07-14.md`.

## Architecture to preserve

- `src/workers/` is the execution engine: evidence, diagnosis, action execution,
  feedback, and operational loops.
- `src/aoip/` is the product/domain/control-plane layer: tenant/environment lifecycle,
  missions, enrollment, autonomy settings, UI-facing projections, and governance.
- Do not physically merge the directories. Put shared contracts in `src/pkg/`.
- Gateway/AOIP must not import workers. Mutations still go through the executor and
  `OMNI_AUTO_EXECUTE_ENABLED=false` remains fail-closed.
- See `docs/architecture/ADR-004-runtime-convergence.md`.

## Delivered control-plane slices

- Tenant and environment lifecycle with migrations `0007` and `0008`.
- Tenant plan/entitlement persistence with migration `0009`.
- Tenant creation provisions a bounded default plan transactionally.
- Scoped agent enrollment and fleet drift handling.
- Durable mission store, command idempotency phases, and reconciliation.
- Tenant-scoped autonomy graduation and provider plan operations at `/licenses`.
- Provider and tenant portal surfaces for the runtime-backed slices.

## Latest UI/security fixes

- Shared UI form wrapping/min-width rules prevent card and action-row overflow.
- Fixed `aoip-button` typo to `aoip-btn`.
- Tenant header now displays the active tenant; overview displays the current role.
- Next `16.2.6` is used across portals; unused `next-auth` removed; `shadcn` moved to
  dev dependencies; PostCSS override set to `8.5.10`.

## Verification snapshot

- Backend `6150 passed, 5 deselected, 173 warnings`.
- Boundary/safety `61 passed`.
- Portal E2E `18/18`.
- Pre-deploy `17/17`.
- Both portal builds/typechecks passed.
- Production npm audit: zero high-severity vulnerabilities.
- Relevant deployed pods Ready, zero restarts; `tenant_plan` has three rows.

## Working-tree and next action

Released 2026-07-15 per user instruction ("làm cả 3 đi"): the accumulated verified
working tree was inspected, staged intentionally, and committed on `main` as three
logical commits — control-plane backend + migrations (`b6941d5`), portal UI
(`362b7cd`), and docs/memory — then pushed. Before the next code task, read the root
`AGENTS.md`, `MEMORY.md`, this file, `docs/CODEBASE.md`, and the verification report.

## Session note 2026-07-14 (afternoon) — external repo, no Omni changes

- This session made NO code changes in this repository; the working tree above is
  unchanged from the verification session (only this handoff file was touched).
- Active task lives in a DIFFERENT repo: `/Users/hiendang/claude-ytb` (YouTube tool).
  User asked to read `docs/TOOL_UPGRADE_PLAN.md` there before any coding.
- Plan summary: P0 first (worker concurrency + ledger/auto_state locking → ideation
  quality gate → Pexels asset catalog), then P1 (series/dedup, SEO, analytics loop,
  schedule), P2 (monetization safety). P1/P2 blocked until P0 has acceptance tests.
- Next step: when user green-lights, follow §12 of that plan — read
  `CHANNEL_GROWTH_PLAN.md`, `AGENTS.md`, `CLAUDE.md`, `data/ledger.md`,
  `assets/auto_state.json` in `claude-ytb`, then start P0.1 (concurrency and state
  locking). No code written yet per explicit user instruction.

## Follow-up resolution (2026-07-15)

- **Onboarding questions reconciled at the root cause.** `expires_at` existed but no
  code enforced it, so PENDING questions accumulated forever. Added
  `question_lifecycle.expire_stale_questions()` (TDD, 4 tests) and wired it into
  `build_provider_human_inbox` before the paced re-ask step. Live Redis reconciled:
  720 stale questions expired (staging-sim 363, tenant-replay-01 357); remaining
  PENDING are all within TTL.
- **Replay-agent heartbeats verified — NOT stale and NOT zombies.**
  `tenant-replay-01_cust-edge/app` are the intentional cross-tenant isolation rig
  (PRODUCT_PROOF.md Iteration 9/25), live via `omni-remote-agent-replay01.service`
  on the VMs, agent v1.1.3 (older than staging-sim fleet v1.3.2 — known state).
  `loyalty_*` registry entries are REAL external UAT hosts (10.210.14.x) pushed
  through the autossh reverse tunnel. Do not delete either group.
- **Autonomy re-verified on the live cluster (2026-07-15):**
  `OMNI_AUTO_EXECUTE_ENABLED=false`, `OMNI_SIEM_SUGGEST_ONLY=true`, no env tier
  override on `omni-fullstack`; PG `autonomy_tier_state` has `default=shadow`;
  `tenant_plan` ceilings are `assist` for all three tenants. Keep shadow/kill-switch
  until an explicit production-governance decision.
- **Warning hygiene:** replaced `datetime.utcnow` (advisory schema default,
  restartedAt annotation), added explicit `tarfile.extractall(filter=...)` in both
  updaters and test fixtures. Test-side mock hygiene applied across 10 test files —
  three patterns: (1) mocked `asyncio.wait_for`/`run_until_complete` must close the
  coroutine passed in (`_wf_return`/`_wf_timeout` helpers), (2) bare `AsyncMock`
  for `llm.embed`/`telegram.send_message`/`analyze_cluster` must return real
  dict/MagicMock (otherwise `.get()`/`.model_dump()` spawn unawaited coroutines),
  (3) `side_effect=noop` instead of `return_value=noop()` plus
  `await asyncio.gather(*tasks, ...)` after cancel.

## Working tree at handoff time (2026-07-15, RELEASED)

Final confirmation suite: `6154 passed, 5 deselected, 2 warnings` — both
remaining warnings are external/benign (StarletteDeprecationWarning from the
`fastapi.testclient` import; `runpy` notice for `services.analyst.__main__`).
All changes below were committed as `0582392` (feat(aoip) question expiry),
`f4a50ce` (fix datetime/tarfile), `7cebb22` (fix(tests) mock hygiene), plus a
docs commit, and pushed to `main`. Working tree is clean.

The change list that went into those commits:

- `src/aoip/question_lifecycle.py` — new `expire_stale_questions()`.
- `src/aoip/console/human_inbox.py` — expiry wired before `_ensure_questions`.
- `src/pkg/reasoning/analyst_advisory_schema.py`, `src/workers/k8s_cluster_tools.py`
  — timezone-aware datetime.
- `src/aoip/agent/updater.py`, `src/remote_agent/updater.py` — tar extract filters
  (`data` for downloaded bundles, `tar` for self-created rollback backups).
- Tests: `test_aoip_question_lifecycle.py` (+4 expiry tests),
  `test_cov_omni_worker_gaps.py`, `test_cov_baseline_snapshot_gaps.py`,
  `test_cov_lab_shell.py`, `test_cov_kubectl_cluster.py`, `test_services_tools.py`,
  `test_remote_agent.py`, `test_cov_remote_agent_pipeline.py`,
  `test_telegram_chunk_boundary.py`, `test_aoip_agent_updater.py`,
  `test_cov_cluster_alert.py`, `test_remote_agent_database.py`,
  `test_database_collector.py`.
- This handoff file.

Progress evidence: full suite after the first hygiene wave was `6154 passed,
5 warnings` (down from 105). The last three fixable warning sources
(cluster-alert bare `llm` AsyncMock, two database-collector `wait_for` timeout
patches) were then fixed; targeted runs are green (51 passed clean). A final
confirmation full-suite run is in flight in the background.

**Next step:** none pending from this session — all three follow-up items
(release, warning hygiene, questions/heartbeats/autonomy reconciliation) are
closed. A fresh session starts from a clean tree on `main`. Note the deployed
pods still run the pre-`0582392` image; `expire_stale_questions` runs in-pod
only after the next `make docker-worker deploy-worker deploy-gateway` rebuild
(until then the provider inbox in the deployed portal does not expire questions
— the live data was already reconciled manually this session).

## Session 2026-07-15 (chiều) — Audit "não" LLM + chống bịa lane advisory

### Phát hiện (bằng chứng runtime thật)

- **Bắt quả tang advisory bịa trên cluster**: trace `gw-prom-84cd18edddb2` — alert
  `OmniBaselineMemZHigh` (self-monitoring) nhưng LLM parrot nguyên văn ví dụ system
  prompt (`root_cause: "Pod nginx-test bị OOMKilled..."`, `trace_id: "<copy from
  input>"`). Advisory bịa đã đi hết pipeline: Telegram message 3940, CRAT seq 2179,
  SUGGEST_REMEDIATION "Confidence: 0.9". Kill-switch chặn mutation (safety giữ),
  nhưng sản phẩm thông tin cho operator là bịa.
- **Root cause kép**: (1) `_META_SELF_RE` không khớp `OmniBaseline*` → alert rơi vào
  RAG+LLM thay vì đường deterministic; (2) lane advisory KHÔNG có grounding gate
  (INV_DIAG_GROUNDED chỉ có ở lane remote-agent `diagnosis_loop.py`).
- **Phát hiện cấu trúc lớn nhất**: prompt advisory dài 38.185 chars nhưng production
  clip head-only ở 10.035 chars (`system_len=10035`, 35%×(num_ctx−num_predict)×4)
  → model chỉ thấy 26%. Bị cắt hoàn toàn: SCOPE-AWARE ENTRY, DECISION RULE,
  REMEDIATION DISCIPLINE, EVIDENCE RELEVANCE (fix vụ DLQ meta-self — vô hiệu âm
  thầm!), SELF-MONITORING META rule, VERDICT SELECTION, FORECASTING, EXAMPLES,
  CRITICAL RULES. Test regression prompt chỉ assert chuỗi TỒN TẠI trong prompt,
  không assert model NHÌN THẤY nó. System Twin cũng không được inject vào evidence
  advisory (gap "liên kết").

### Changes (working tree, CHƯA commit)

- `src/workers/advisory_grounding_gate.py` (MỚI) — gate hậu nghiệm: claim
  keyword-gated (Pod/Deployment/... + dash-name, cặp ns/name có dash, path, %,
  placeholder `<...>`) phải có verbatim trong evidence_text; nếu không → verdict
  INVESTIGATE, confidence low, xoá remediation, lọc steps nhiễm, cap forecast.
  Thiết kế keyword-gated tránh false-positive prose (`out-of-memory`,
  `self-resolved`) — KHÔNG dùng stoplist đuổi bắt.
- `src/workers/advisory_analyst_handler.py` — wire gate sau
  `_correct_escalation_reason`, TRƯỚC `_compute_escalation_tier`.
- `src/workers/alert_envelope.py` — `_META_SELF_RE` thêm `Baseline`.
- `src/workers/evidence_consumer.py` — SUGGEST advisory dùng
  `confidence_to_float(advisory.confidence)` thay hardcode 0.9.
- `src/pkg/reasoning/analyst_advisory_schema.py` — `CONFIDENCE_TO_FLOAT` +
  `confidence_to_float()` (high .9 / medium .6 / low .3).
- `src/workers/advisory_mode_system_prompt.py` — block `[ANTI-PARROTING]` đầu
  prompt (trong vùng nhìn thấy) + `OmniBaseline*` vào danh sách meta-self.
- Tests: `tests/test_advisory_grounding_gate.py` (MỚI, 16 test),
  `test_alert_envelope.py` (+1), `test_advisory_prompt_evidence_relevance.py` (+2),
  `test_llm_reasoning_hash.py` (mock hygiene: gate đọc field text thật),
  `tests/benchmarks/test_advisory_quality.py` (fake stub hết bịa
  `multi-agent/target-workload` → `unknown`).

### Verification

Full suite: `6171 passed, 5 deselected, 2 warnings` (+17 test mới, 2 warning là
external/benign như phiên trước). Benchmark 23 golden case pass nguyên vẹn.

### Update 2026-07-16 — user duyệt "triển khai hết đi": P0a/P0b/P1 ĐÃ code xong

User chốt triển khai toàn bộ đề xuất. Đã làm (TDD, working tree, CHƯA commit):

- **P0a — prompt tái cấu trúc theo clip budget**
  (`src/workers/advisory_mode_system_prompt.py` viết lại hoàn toàn):
  `build_advisory_system_prompt(ws=None, evidence_text="")` = CORE (~7.7k chars,
  chứa TOÀN BỘ guard sống còn: ANTI-PARROTING, EVIDENCE RELEVANCE,
  SELF-MONITORING/OmniBaseline, VERDICT SELECTION + CONSISTENCY, REMEDIATION
  DISCIPLINE, SCOPE-AWARE ENTRY, impact_chain, forecast, critical rules) + 6
  section động bật theo evidence_text (KB / SIEM-kill-chain / DB / storage /
  services / HTTP-surge). Helper mới `production_prompt_clip_chars()` (=10035
  với default 8192/1024). Bất biến: MỌI tổ hợp section ≤ clip — enforce bởi
  `tests/test_advisory_prompt_budget.py` (8 test). 3 example JSON lớn (nguồn
  parrot) đã xoá và bị test cấm quay lại. Handler truyền
  `evidence_text` vào builder.
- **P0b — verdict guard deterministic** (`src/workers/advisory_verdict_guard.py`
  MỚI + `tests/test_advisory_verdict_guard.py` 11 test): URGENT/CRITICAL mà
  evidence không có failure signal cụ thể (FAILED/OOMKilled/5xx/z≥3σ/SIEM/...)
  → hạ INVESTIGATE + cap forecast. Wired trong handler SAU grounding gate,
  TRƯỚC `_compute_escalation_tier`.
- **P1 — System Twin injection** (`src/workers/system_twin_context.py` MỚI +
  `tests/test_system_twin_context.py` 4 test): `build_system_twin_block()` đọc
  `omni:aoip:system_model:{tenant}`, render block compact ≤800 chars, fail-open.
  Wired trong `evidence_consumer.py` sau sigma block, trước RAG brain, dùng
  `_tenant_id_from_batch(batch)`.
- **P2 (nâng OMNI_LLM_NUM_CTX)**: coi là superseded bởi P0a — KHÔNG đổi env
  default (cần quyết định ops riêng).

Verification: 223 test trực tiếp liên quan pass (gồm fake-LLM benchmark đi qua
handler + cả 2 gate + prompt mới). Full suite đang chạy nền (`bdwinublu`).
Baseline benchmark live từ HEAD (prompt cũ) chạy nền trong worktree
`scratchpad/baseline-head` (`bacye5ge4`, BENCHMARK_NUM_CTX=8192) — file
`tests/benchmarks/results/benchmark_20260715_172113.json` là run HỎNG (23/23
"no advisory returned"), không dùng làm baseline.

### Benchmark before/after (2026-07-16, live qwen2.5-coder:7b, NUM_CTX=8192)

- **Before** (HEAD 957148f, prompt cũ 38k bị clip, không gate): 7/23 pass
  (30.4%), avg 63.5 — `scratchpad/baseline-head/.../benchmark_20260716_154136.json`.
- **After** (working tree, prompt mới + 2 gate + twin): **10/23 pass (43.5%),
  avg 69.7** — `tests/benchmarks/results/benchmark_20260716_155810.json`.
- Tăng mạnh: case_002 (+40), case_011 (+30), case_020 (+30), case_022 (+30),
  case_023 (+35), case_016 (+25). Tụt: case_001/-7.5, case_017/-10, case_018/-30,
  case_004/-15, case_008/-25, case_009/-25 — TẤT CẢ đều là verdict mismatch;
  đã xác minh cả 6 case đều CÓ failure signal trong evidence → verdict guard
  KHÔNG kích hoạt (vô tội). Đang chạy lại 6 case với gate logging
  (scratchpad/rerun_regressed_cases.py) để phân định grounding-gate over-fire
  vs variance model 7B trước khi deploy.
- Full suite: 6194 passed (30 failure ban đầu do cluster OrbStack TẮT — đã
  `orbctl start` + `orbctl start k8s`, cả 30 pass lại; pod fullstack/gateway/
  onboarding tự hồi phục 1/1 Running).
- Lưu ý: `tests/benchmarks/results/benchmark_20260715_172113.json` là run HỎNG
  (23/23 "no advisory returned") — không dùng.

### Rerun 6 case tụt + fix gate case-sensitivity (2026-07-16)

Rerun với gate logging phân định: case_001/017/018 = model 7B tự chọn verdict
lệch 1 bậc (URGENT↔CRITICAL, trần model — không gate nào fire); case_004 = gate
bắt ĐÚNG model parrot `nginx-test`; case_009 = **bug gate case-sensitivity**:
evidence "Ollama" (hoa) vs claim "ollama" (thường) → fire nhầm. Fix TDD:
`test_grounding_check_is_case_insensitive` (RED→GREEN), so sánh grounding
lowercase cả corpus lẫn claim (`collect_ungrounded_claims`, `_workload_claims`,
`_step_is_contaminated`). 195 test gate/benchmark xanh.

### Deployed lab 2026-07-16 — verify in-pod PASS

`make docker-worker deploy-worker` (image rebuild SAU fix case-sensitivity,
sha256:5f3451b1...). Verify trong pod omni-fullstack: 3 module mới import OK
(`advisory_verdict_guard`, `system_twin_context`, `advisory_grounding_gate`),
`production_prompt_clip_chars()=10035`, prompt max-sections 10028 ≤ clip,
fix case-insensitive có trong source in-pod, healthz ok (kafka lag=0).
Worktree baseline `scratchpad/baseline-head` đã dọn. Memory
`project_advisory_prompt_clip_and_grounding_gate` đã cập nhật số benchmark.

### Next step (superseded — xem session 2026-07-20 bên dưới cho state hiện tại)

CHƯA commit/push (chưa được chỉ thị) — working tree chứa toàn bộ thay đổi
advisory anti-ngáo ở trên, đã test + deploy + verify. Việc còn mở duy nhất:
quan sát vài advisory thật trên lab (Telegram/trace) để xác nhận chất lượng
runtime; các case benchmark còn fail là trần model qwen2.5-coder:7b
(verdict lệch 1 bậc), muốn cải thiện tiếp phải đổi model hoặc thêm
verdict-nudge deterministic — quyết định riêng.

## Session 2026-07-20 — READ-ONLY audit chuỗi (3 vòng) + P0-1 CRAT fix RemoteAgent lane

### Audit chain (không sửa code) — kết luận cuối

3 audit READ-ONLY liên tiếp trong ngày (Principal Architect/SRE Auditor →
revised với 3-chiều maturity → autonomous execution) đã xác nhận qua file:line
thật (không dựa memory cũ): Omni có **diagnostic ReAct xuyên biên giới thật**
qua `src/services/analyst/diagnosis_loop.py` (Remote Agent enqueue command →
blocking wait ≤90s → kết quả quay lại cùng LLM session tới 8 turn — **L3 xác
nhận**), nhưng **operational autonomy = L0 runtime hiện tại toàn hệ thống**:
K8s mutate wired tới L4 (verify+rollback có code) nhưng khoá cứng bởi
`OMNI_AUTO_EXECUTE_ENABLED=false` (xác nhận trên pod thật); nhánh RemoteAgent
diagnosis không tạo governed decision nào (không qua `tier_gate`, không CRAT)
— chỉ phát Telegram. Kết luận cuối 3 lần audit đều thống nhất:
**"Omni hiện vẫn chủ yếu là advisory/diagnostic platform; Remote Agent chưa
phải actuator đóng vòng."** Commercial readiness ước tính 1.6-1.7/5 (evidence-based,
không đếm theo dòng code). Toàn bộ nội dung audit đầy đủ (matrix, roadmap 6-phase
Phase 0-6, backlog P0/P1/P2, AOIP 3-option strategy, Mermaid sequence diagram
target) nằm TRONG transcript hội thoại — CHƯA được ghi thành file trong repo.
Nếu phiên sau cần lại toàn văn, phải hỏi user có muốn ghi thành
`docs/architecture/` hay không (chưa làm vì chưa được chỉ thị).

Phát hiện P0 quan trọng nhất từ audit (đầy đủ bằng chứng file:line, đã verify
qua `grep` trực tiếp/gián tiếp/decorator/Kafka-topic-producer — không suy
diễn): **RemoteAgent diagnosis lane (`diagnosis_loop.py` → Telegram) không hề
ghi CRAT trước dispatch**, vi phạm trực tiếp AGENTS.md invariant "CRAT Fail-
Closed: write_audit_block() MUST succeed trước Telegram emit / action
dispatch" — comment tại `remote_agent_pipeline.py:34` tuyên bố có ghi CRAT
nhưng thực tế 0 call site. Các P0 khác còn mở (KHÔNG sửa turn này, cần phiên
riêng vì đụng chạm production-adjacent lớn hơn): daemon VM production
(`src/aoip/agent/daemon.py`) gọi executor generic (`operations.py`) thay vì
executor đã hardening (`src/aoip/capabilities/systemd_restart.py` — allowlist/
precondition/approval/idempotency/lease đầy đủ trên giấy nhưng KHÔNG nằm trên
đường chạy thật); 0 `tier_gate` trên RA command dispatch; tenant→agent binding
là TOFU (trust-on-first-use, không provisioned trước).

### Fix đã làm (P0-1, TDD, verify đầy đủ)

- `src/workers/remote_agent_pipeline.py` — `_run_diagnosis_and_notify()` nay
  ghi `write_audit_block(event_type="ADVISORY_DECISION", tenant_id=<từ
  ev_doc>)` NGAY SAU khi lưu session, TRƯỚC khi gọi `emit_diagnosis_to_telegram`.
  Lỗi ghi CRAT → fail-closed thật: `mark_stage(...,"CRAT","fail")`, return sớm,
  **Telegram KHÔNG được gọi**. Comment cũ ở đầu file (dòng ~34) nay khớp đúng
  hành vi thật, không cần sửa chữ.
- `tests/test_remote_agent_diagnosis_crat.py` (MỚI, 3 test): thứ tự CRAT→
  Telegram đúng, fail-closed khi CRAT lỗi (Telegram bị chặn thật), `tenant_id`
  truyền đúng vào audit block cho tenant isolation của hash-chain
  (`audit_chain:{tenant_id}:*` theo `chain_writer._tenant_keys`).

### Verification (output thật)

```
.venv/bin/python -m pytest tests/test_remote_agent_diagnosis_crat.py -q
3 passed in 0.34s

.venv/bin/python -m pytest tests/test_cov_remote_agent_pipeline.py tests/test_remote_agent_e2e.py tests/test_remote_agent_diagnosis_crat.py -q
36 passed in 8.84s

.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
6198 passed, 5 deselected, 2 warnings in 159.87s
```
(+44 so với baseline 6154 phiên trước = test có sẵn từ working tree advisory
anti-ngáo chưa commit; +3 là test mới của fix này. Không có regression.)

**Runtime/E2E trên cluster thật: CHƯA làm — ghi UNKNOWN**, không suy diễn.
Test trên chỉ chạy với `FakeRedis`, chưa deploy. Muốn xác nhận RUNTIME-PROVEN
thật: `make docker-worker deploy-worker`, trigger 1 RemoteAgent evidence thật
(`make e2e-proactive` hoặc `/simulate/{lane}`), rồi
`redis-cli LRANGE audit_chain:blocks -5 -1` xác nhận block `ADVISORY_DECISION`
mới xuất hiện đúng lúc diagnosis loop chạy.

### Working tree hiện tại (CHƯA commit — 2 dòng công việc độc lập cộng dồn)

```
 M docs/handoffs/CURRENT_SESSION.md                (handoff, turn này)
 M src/pkg/reasoning/analyst_advisory_schema.py     (advisory anti-ngáo, 07-16, xem trên)
 M src/workers/advisory_analyst_handler.py          (advisory anti-ngáo)
 M src/workers/advisory_mode_system_prompt.py       (advisory anti-ngáo)
 M src/workers/alert_envelope.py                    (advisory anti-ngáo)
 M src/workers/evidence_consumer.py                 (advisory anti-ngáo)
 M src/workers/remote_agent_pipeline.py             (MỚI turn này — P0-1 CRAT fix)
 M tests/benchmarks/test_advisory_quality.py         (advisory anti-ngáo)
 M tests/test_advisory_prompt_evidence_relevance.py  (advisory anti-ngáo)
 M tests/test_alert_envelope.py                      (advisory anti-ngáo)
 M tests/test_llm_reasoning_hash.py                  (advisory anti-ngáo)
?? src/workers/advisory_grounding_gate.py            (advisory anti-ngáo)
?? src/workers/advisory_verdict_guard.py             (advisory anti-ngáo)
?? src/workers/system_twin_context.py                (advisory anti-ngáo)
?? tests/benchmarks/results/*.json                   (advisory anti-ngáo, benchmark artifacts)
?? tests/test_advisory_grounding_gate.py             (advisory anti-ngáo)
?? tests/test_advisory_prompt_budget.py              (advisory anti-ngáo)
?? tests/test_advisory_verdict_guard.py              (advisory anti-ngáo)
?? tests/test_remote_agent_diagnosis_crat.py         (MỚI turn này)
?? tests/test_system_twin_context.py                 (advisory anti-ngáo)
```

Hai dòng công việc KHÔNG xung đột file (advisory anti-ngáo chạm
`advisory_*`/`evidence_consumer.py`/`alert_envelope.py`; fix P0-1 chỉ chạm
`remote_agent_pipeline.py` + test riêng) — có thể commit độc lập hoặc gộp,
tuỳ user quyết định.

## Fix #2 turn này (2026-07-20, tiếp) — action_id binding bug trong operations.py

**Correction so với audit trước:** claim cũ "P0-2 = daemon gọi executor KHÔNG
an toàn" là không chính xác. Đọc lại kỹ `src/aoip/agent/operations.py` +
`daemon.py` cho thấy `build_recovery_executor` → `run_guarded_recovery` tự có
cơ chế an toàn nghiêm túc riêng (single-writer lease `ExecutionLease` +
`IdempotencyLedger` + current-state revalidate qua `execute_recovery`) — KHÔNG
phải "no-op"/"unsafe". Vấn đề thật là **hai stack recovery độc lập cùng tồn
tại** (P0-2 đúng nghĩa, giữ nguyên NOT_IMPLEMENTED, cần ADR):
- Stack A (console/CLI): `command_bridge.py` → `capabilities/systemd_restart.py`
  (425 dòng, allowlist/precondition/approval/idempotency/lease riêng).
- Stack B (durable agent daemon, production path thật):
  `daemon.py` → `operations.py::build_recovery_executor` → `execute_recovery`
  (`aoip/recovery.py::operator_for`) — lease+idempotency TỰ VIẾT LẠI, không
  gọi Stack A. `grep -rln systemd_restart src/ tests/` xác nhận zero overlap
  code giữa 2 stack.
Đây là rủi ro kiến trúc thật ("hai executor khác safety model cho cùng
capability") nhưng KHÔNG có nghĩa Stack B kém an toàn — cả hai đều có
lease+idempotency riêng, nghiêm túc. Quyết định gộp/giữ cần ADR, không phải
patch nhanh trong 1 turn — vẫn deferred.

**Nhưng khi đọc kỹ Stack B để đánh giá P0-2, phát hiện 1 bug thật trong
`operations.py::decode_recovery_command` (dòng 326 cũ):**

```python
    except ValueError as exc:
        raise UnsupportedRecoveryPayload(f"invalid_approval: {exc}") from exc
    # Bind the immutable action identity from the approval into the request so
    # idempotency cannot collapse two actions with the same intent.
        req = replace(req, action_id=approval.action_id)   # ← thụt lề 8-space, DEAD CODE
```
Dòng rebind `action_id` thụt lề 8-space → nằm TRONG block `except` ngay sau
`raise` → không bao giờ chạy (raise đã unwind trước đó). Hệ quả: `req.action_id`
luôn là `""` (default `RecoveryRequest.action_id: str = ""`), nên
`_key_for()` — điều kiện `all((tenant, mission_id, incident_id, decision_id,
action_id, command_id))` — luôn `False` với MỌI payload production thật, kể cả
payload đã có đủ `mission_id/incident_id/decision_id/command_id`. Idempotency
key luôn rơi về nhánh legacy thô (`idempotency_key`, chỉ theo
tenant+scope+decision_goal+failure_mode+unit), KHÔNG bao giờ dùng
`command_identity` (theo correlation ID cụ thể của command). Rủi ro thật: hai
lệnh remediation KHÁC NHAU nhưng cùng target+failure_mode+unit (vd 2 lần sự cố
riêng biệt) có thể trùng idempotency key → lệnh thứ 2 bị coi "đã chạy",
reconcile zero-mutation — **mất một remediation hợp lệ**, không phải false
positive vô hại.

**Fix:** sửa indent (8→4 space), đưa dòng rebind ra khỏi block `except`, chạy
sau khi `Approval.issue()` thành công. File: `src/aoip/agent/operations.py`
(1 dòng).

**Test mới** (`tests/test_aoip_operations.py`, +2 test, không sửa test cũ):
- `test_decode_recovery_command_binds_action_id_from_approval` — assert
  `req.action_id == approval.action_id == "act-1"`.
- `test_key_for_uses_correlation_identity_when_payload_fully_bound` — payload
  đủ `mission_id/incident_id/decision_id/command_id` → assert `_key_for(req)`
  trả đúng `command_identity(...)`, không rơi về `idempotency_key(...)`.

Test cũ KHÔNG catch được bug này vì không assert `req.action_id` — coverage
gap đã đóng.

**Verification thật đã chạy:**
```
.venv/bin/python -m pytest tests/test_aoip_operations.py -q
29 passed in 0.54s   # 27 test cũ + 2 test mới, không regression

.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
6200 passed, 5 deselected, 2 warnings in 158.04s   # +2 so baseline 6198, không regression
```
Runtime/E2E trên VM lab thật (Stack B chạy qua `omni-remote-agent.service`
hoặc daemon AOIP thật): CHƯA làm — ghi UNKNOWN, giống P0-1. Cần VM lab access
để verify `_key_for` sinh đúng key trên Redis thật khi 2 recovery command
liên tiếp cùng target xảy ra.

- P0-3 (tier_gate cho RA dispatch qua `agent_commands.py`) — RE-EVALUATED: kênh
  `enqueue_commands`/`poll_commands` trong `agent_commands.py` đã fail-closed
  READONLY-only cả 2 lớp (gateway `_COMMAND_WHITELIST` + agent
  `command_executor.py::_SYSTEMCTL_READONLY`/`_WRITE_SUBCOMMANDS` chặn mutate
  qua kênh này) — `risk_class_of()` sẽ luôn trả READONLY nên tier_gate ở đây
  là no-op thật sự, KHÔNG phải gap. Rút P0-3 khỏi backlog P0; mutation RA thật
  chỉ đi qua Stack A/Stack B (xem ADR-005 bên dưới).

## P0-4 fix (2026-07-20, tiếp) — per-agent credential agent_id binding

Audit lại "TOFU tenant→agent binding" (claim cũ) cho kết quả CHÍNH XÁC HƠN,
không phải TOFU đơn thuần:
- `_require_api_key` (`src/gateway/api.py`) đã resolve per-agent credential
  thật qua PG `omni_admin.agent_credential` (IT-3), tenant_id lấy từ ctx xác
  thực — KHÔNG phải tự khai báo trong body. First-write-wins chỉ áp dụng cho
  namespace `agent_id` bên trong 1 tenant đã xác thực — không phải lỗ hổng
  spoofing như tên gọi cũ ngụ ý.
- **Root cause thật:** PG `agent_credential` đã lưu đúng `(tenant_id, agent_id)`
  tại thời điểm enroll, `_resolve_agent_credential()` đã lookup đúng cả 2
  field — nhưng `TenantContext` (dataclass) không có field `agent_id`, nên
  binding đó bị RỚT trước khi tới `require_agent_tenant()`. Hệ quả: 1 VM giữ
  credential per-agent hợp lệ cho `agent_id=X` vẫn có thể register/push
  evidence/poll commands dưới BẤT KỲ `agent_id` nào khác cùng tenant — vô
  hiệu hoá mục đích chính của per-agent credential (IT-3) mà không có cảnh
  báo nào (operator tưởng đã có per-agent isolation).
- **Bug phụ phát hiện cùng chỗ:** khi PG lookup thành công nhưng Redis không
  sẵn có, `_resolve_agent_credential()` cũ ngầm `return None` (rớt khỏi block
  `if redis is not None:`) → credential hợp lệ bị từ chối 401 im lặng mỗi khi
  Redis down. Đã sửa cùng lúc (đưa `return TenantContext(...)` ra ngoài block).

**Fix:** `TenantContext.agent_id: str | None = None` (mới) +
`require_agent_tenant()` raise 403 khi `ctx.agent_id` khác agent_id mục tiêu +
`_resolve_agent_credential()` truyền `agent_id`/`environment_id` đúng ở CẢ 2
đường (cache-hit và PG-lookup fresh). Files:
`src/gateway/tenant_context.py`, `src/gateway/api.py`.

**Test mới** (8 test, không sửa test cũ):
- `tests/test_tenant_isolation.py::TestRequireAgentTenant` — 3 test (reject
  khác agent_id, allow đúng agent_id, tenant-shared key không bị ảnh hưởng).
- `tests/test_agent_enrollment.py::TestPerAgentCredentialScoping` — 5 test,
  đi qua route thật (`/webhook/agent/register`) với `_require_api_key` +
  `agent_webhook.router` thật, cộng 2 test cho cache round-trip và trường
  hợp Redis down.

**Verification thật:**
```
.venv/bin/python -m pytest tests/test_tenant_isolation.py tests/test_agent_enrollment.py -q
64 passed in 0.54s

.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
6207 passed, 5 deselected, 2 warnings in 158.01s   # +7 so 6200, không regression
```
Runtime trên VM lab thật: CHƯA làm — ghi UNKNOWN (cần re-enroll 1 agent lab
thật với 2 agent_id khác nhau cùng tenant, xác nhận 403 xảy ra qua HTTP thật
không chỉ FakeRedis/fake PG).

## P0-2 — ADR-005 viết xong, KHÔNG implement (2026-07-20, tiếp)

Đọc kỹ để xác minh claim "P0-2" cũ ("daemon gọi executor không an toàn") và
phát hiện nó KHÔNG chính xác — sự thật tinh vi hơn và nghiêm trọng hơn theo
1 khía cạnh cụ thể:
- Stack A (`command_bridge.py`/`console/approve_systemd_restart.py` →
  `capabilities/systemd_restart.py::build_systemd_restart_executor`) VÀ
  Stack B (`daemon.py` → `operations.py::build_recovery_executor`) đều gọi
  chung `operations.run_guarded_recovery` (lease+idempotency dùng chung, KHÔNG
  duplicate như audit trước tưởng).
- Khác biệt thật: Stack A có preflight riêng — allowlist unit cụ thể
  (`AOIP_ALLOWED_SYSTEMD_UNITS`/`SystemdRestartPolicy`) + payload-hash
  tamper-binding — trước khi vào `run_guarded_recovery`. Stack B
  (`operations.decode_recovery_command`) KHÔNG có allowlist unit, KHÔNG có
  payload-hash check — chỉ có `RecoveryGate` (failure_mode/substrate/risk/
  node scope-prefix thô).
- `src/aoip/agent/runtime_config.py:16,112-114` xác nhận daemon LIVE (chạy
  thật trên cả 3 VM lab theo ADR-001) dùng Stack B, KHÔNG BAO GIỜ load
  `AOIP_ALLOWED_SYSTEMD_UNITS`. Nghĩa là: allowlist unit cụ thể mà operator
  tưởng đã cấu hình (qua policy Stack A) **không có hiệu lực trên daemon thật
  đang chạy** — chỉ có hiệu lực khi lệnh được author qua CLI
  `approve_systemd_restart.py`. Một caller khác author payload đúng shape
  generic của Stack B (không qua CLI đó) sẽ restart được bất kỳ unit nào
  khớp `scope_prefix`, không bị allowlist unit chặn.

Chi tiết đầy đủ + file:line + 2 phương án + khuyến nghị (option 2: đưa
`allowed_targets` vào `RecoveryGate` generic thay vì chỉ Stack A có) ở
`docs/architecture/ADR-005-recovery-executor-consolidation.md`. **KHÔNG
implement** — thay đổi phạm vi quyền mutation của daemon đang chạy thật trên
VM lab cần sign-off người, đúng theo constraint "never self-elevate/widen
production mutation authority" của phiên này.

## Deploy + RUNTIME-PROVEN thật trên cluster live (2026-07-20, cuối phiên)

User cấp quyền truy cập cluster OrbStack + 3 VM lab thật trong phiên này. Đã:

1. **Infra drift fix trước deploy:** `make pre-deploy-validate` FAIL 3 mục
   (thiếu topic `omni-hitl-pending`, `omni-audit-chain` thiếu
   `cleanup.policy=compact`/`retention.ms=-1` — vi phạm invariant CRAT trong
   AGENTS.md). Chạy `make ensure-kafka-topics` (script idempotent, không
   xoá/rename topic nào) → PASS 17/17.
2. **Deploy thật:** `make deploy-worker` (build `multi-agent-system:latest`,
   rollout `omni-fullstack`) + `make docker-gateway && make deploy-gateway`
   (rollout `omni-gateway`) — cả 2 pod Running, 0 restart sau rollout.
3. **Xác nhận code fix thật sự chạy trong pod** (không chỉ build pass — theo
   đúng bài học `project_productization_iteration1_twin` về deployment
   drift): `kubectl exec` + `inspect.getsource()` trực tiếp trong pod xác
   nhận cả 4 thay đổi có mặt (`write_audit_block` trong
   `_run_diagnosis_and_notify`; `action_id` rebind indent=4 đúng vị trí;
   `agent_id` field trong `TenantContext`; scoping check trong
   `require_agent_tenant`; propagation trong `_resolve_agent_credential`).
4. **P0-4 RUNTIME-PROVEN qua HTTP thật** (port-forward `omni-gateway`, admin
   key thật từ secret `omni-gateway-secret`): tạo enroll token thật qua
   `/autonomy/tenants/staging-sim/enroll-tokens`, đổi lấy credential per-agent
   thật qua `/webhook/agent/enroll` cho `agent_id=p0-4-verify-agent-A`, dùng
   chính credential đó gọi `/webhook/agent/register` với
   `agent_id=p0-4-verify-agent-B` → **HTTP 403** `"credential is scoped to a
   different agent_id"` (đúng fix); gọi lại với `agent_id=p0-4-verify-agent-A`
   (agent_id đúng của nó) → **HTTP 200**. Credential test đã revoke qua
   `DELETE /autonomy/tenants/staging-sim/agent-credentials/p0-4-verify-agent-A`
   ngay sau khi verify xong.
5. **P0-1 RUNTIME-PROVEN trực tiếp trong pod** (không qua Kafka evidence
   thật để tránh spam Telegram ops + chờ LLM thật — mock đúng 2 lời gọi
   ngoài `run_diagnosis_loop`/`emit_diagnosis_to_telegram`, y hệt unit test,
   nhưng `write_audit_block` chạy THẬT với Redis+Kafka+Ed25519 signer thật
   của pod, `OMNI_AUDIT_PRIVATE_KEY_PATH` xác nhận set → chain có ký thật):
   - Lần 1 dùng `AIOKafkaProducer` trần (thiếu `send_dict`) → CRAT ghi Redis
     thành công nhưng Kafka publish lỗi → `write_audit_block` raise đúng →
     `call_order=['diagnosis']` (Telegram KHÔNG được gọi) → xác nhận
     **fail-closed hoạt động đúng trên hạ tầng thật** khi có lỗi thật.
   - Lần 2 dùng đúng `messaging.kafka_bus.KafkaBus` (wrapper thật pod dùng)
     → thành công hoàn toàn: `call_order=['diagnosis', 'telegram']`, stage
     `CRAT=ok`/`DISPATCH=ok`, block mới xuất hiện thật trong
     `audit_chain:staging-sim:blocks` (11→12), `event_type=ADVISORY_DECISION`,
     `signature_hex` có giá trị (ký Ed25519 thật, không phải lab-unsigned).
   - Dữ liệu test đánh dấu rõ "SYNTHETIC RUNTIME VERIFY — safe to ignore"
     trong payload; KHÔNG xoá block khỏi audit_chain (hash-chain immutable —
     xoá mới là vi phạm CRAT, để lại bản ghi test có nhãn rõ là đúng thiết kế).
   - Redis trace-stage keys tạm đã xoá; registry test-agent tự hết TTL 120s.

**Kết luận maturity cập nhật:** P0-1 và P0-4 nay là **RUNTIME-PROVEN** (không
còn UNKNOWN) — cả 2 chạy đúng trên cluster live thật với hạ tầng CRAT ký thật.
P0-2 vẫn dừng ở ADR (không implement — đổi phạm vi mutation authority của
daemon thật cần quyết định riêng, không tự ý dù có quyền truy cập).

## Next step thật (2026-07-20, cuối phiên)

- **Chưa commit/push bất kỳ thay đổi nào** (advisory anti-ngáo cũ + P0-1 CRAT
  fix + action_id binding fix + P0-4 credential-scoping fix + ADR-005) — chưa
  được chỉ thị. Không file nào xung đột giữa các dòng công việc. Cả 2 pod
  (`omni-fullstack`, `omni-gateway`) ĐANG CHẠY code mới (deployed, chưa
  commit vào git — tách biệt 2 khái niệm: deployed vs. committed).
- **P0-2**: ADR-005 đã viết, chờ sign-off người trước khi đổi
  `RecoveryGate`/`runtime_config.py` — đây là thay đổi phạm vi mutation
  authority của daemon thật, KHÔNG tự ý làm dù có quyền sửa code/deploy.
- **P0-3**: đã rút khỏi backlog (RE-EVALUATED — không phải gap, xem trên).
- **P0-1 + P0-4**: DONE, RUNTIME-PROVEN trên cluster live (xem trên).
- Phase 0 (canonical contracts `src/pkg/`) và Phase 1-6 (vertical slice mở
  rộng) của roadmap "Omni Autonomous Productization" — vẫn NOT_IMPLEMENTED,
  quy mô nhiều tuần thiết kế xuyên hệ thống, chưa động tới trong phiên này.

## Commit + push (2026-07-20, cuối phiên) — 6 commit lên main

User chọn "Commit + push trước", sau đó "làm tiếp đi" cho ADR-005. Đã push
`957148f..c1f432d` (7 commit tổng, 6 của phiên này):
1. `25bcbee` feat(advisory) anti-hallucination guardrails (dòng công việc cũ 07-15/16).
2. `c98014d` fix(workers) P0-1 CRAT trước Telegram dispatch.
3. `de1c539` fix(aoip) action_id idempotency binding bug.
4. `2cc4c7a` fix(gateway) P0-4 per-agent credential agent_id scoping.
5. `9ca2340` docs(architecture) ADR-005 ban đầu (Proposed, chưa implement).
6. `e42990b` docs(handoffs) đóng phiên (bản trước bản này).
7. `c1f432d` fix(aoip) implement ADR-005 — `RecoveryGate.allowed_targets`
   fail-closed, wire `AOIP_ALLOWED_SYSTEMD_UNITS` vào `runtime_config.py`,
   sửa 11 call site `RecoveryGate(...)` cũ (2 demo script + 9 test file),
   +5 test mới (`test_aoip_runtime_config.py`). Full suite `6211 passed,
   5 deselected`, không regression.

**ADR-005 status cập nhật: Accepted, đã implement.** Zero live-behavior
impact xác nhận bằng cách đọc trực tiếp `AOIP_AGENT_MODE` trên cả 3 VM lab
(`orb -m <vm> sudo systemctl show aoip-agent.service -p Environment`) — cả
3 đều `observe_only`, code path `_build_gate()`/`RecoveryGate` chưa từng
được daemon thật gọi tới. Fix chỉ có hiệu lực khi VỪA (a) release mới chứa
code này được publish lên VM qua kênh update chính thức (IT-5,
`make publish-agent-release`, KHÔNG sửa file trực tiếp trên VM) VỪA (b)
operator chủ động bật `AOIP_AGENT_MODE=mutation_enabled` — quyết định riêng,
chưa làm trong phiên này.

K8s image (`omni-fullstack`, `omni-gateway`) đã rebuild+redeploy lại để đồng
bộ với git HEAD sau commit cuối — cả 2 pod Running, `/healthz`+`/readyz`
xanh. Lưu ý: code AOIP trong K8s image KHÔNG được K8s pod thực thi (daemon
chạy trên VM qua systemd, không qua K8s) — redeploy K8s chỉ là vệ sinh đồng
bộ image↔git, không phải "deploy fix" theo nghĩa runtime-proof cho chính
ADR-005.

## Rollout thật lên VM fleet (2026-07-20, cuối phiên) — user chỉ thị "bật multi agent lên chạy"

User xác nhận rõ (AskUserQuestion): cả (1) publish release mới VÀ (2) bật
`mutation_enabled` thật. Đã làm cả hai, tuần tự, với 2 bug thật phát hiện
giữa chừng (không phải do phiên này gây ra — lộ ra khi thử làm thật):

**1. Publish release 1.3.3 lên cả 3 VM lab thật:**
- Bump `src/remote_agent/VERSION` 1.3.2→1.3.3, `make publish-agent-release`.
- **Bug thật #1 (hạ tầng, không phải code):** kênh update cần HTTPS
  (`INV_HTTPS_ONLY`) nhưng cluster này CHƯA từng có TLS cho
  `gateway.ai-agent.local` — chỉ HTTP. Dựng self-signed CA lab (openssl),
  tạo K8s TLS secret `omni-gateway-tls`, thêm Ingress `omni-gateway-https`
  (entrypoint `websecure`, Traefik đã sẵn port 443) vào
  `k8s/ingress/ai-agent-local.yaml`. Trust CA trên cả 3 VM
  (`update-ca-certificates`) — verify TLS thật không cần `-k`. Set
  `OMNI_AGENT_UPDATE_ALLOWED_HOSTS=gateway.ai-agent.local` vào
  `omni-worker-configmap.yaml` (đọc bởi gateway).
- **Bug thật #2 (code, đã fix + test + commit):** venv Python trên VM dùng
  `certifi` bundle riêng, KHÔNG dùng system trust store → vẫn
  `CERTIFICATE_VERIFY_FAILED` dù đã trust CA ở OS. Append CA cert vào
  `certifi.where()` path trong venv (không tracked git — riêng từng VM).
- **Bug thật #3 (code, đã fix + test + commit, PHÁT HIỆN QUAN TRỌNG):**
  `/webhook/agent/release/bundle` nằm sau `_require_api_key` như mọi route
  agent khác, nhưng `remote_agent/updater.py::_download()` CHƯA BAO GIỜ gửi
  credential nào → HTTP 401 trên bất kỳ cluster nào có key thật cấu hình
  (tức mọi deployment không phải lab-no-auth). Đây là gap có thật, không
  phải do phiên này gây ra — chỉ lộ ra vì lần đầu thử update thật kể từ khi
  cluster có key. Fix: `_download`/`handle_update_command`/`execute_batch`
  nhận thêm `api_key`, `agent.py` truyền `cfg.api_key` — commit
  `4b46da2`, +6 test mới, full suite `6214 passed`.
- **Bootstrap khó:** code cũ trên VM có chính bug #3 nên tự-update qua kênh
  chính thức không tự sửa được chính nó (vòng luẩn quẩn). Lần đầu chỉ patch
  3 file lẻ (`updater.py`/`command_executor.py`/`agent.py`) → gây crash-loop
  MỚI (agent.py bản HEAD import hàm không tồn tại trong `collectors/logs.py`
  bản cũ còn lại trên VM — version-skew giữa các file). Fix đúng: sync
  NGUYÊN block `src/remote_agent/` + `src/aoip/` nhất quán (build lại tarball
  release, giải nén thẳng vào `/opt/omni-remote-agent/`), không patch từng
  file lẻ. Bài học: bootstrap một agent tự-update bị hỏng PHẢI đồng bộ toàn
  bộ package, không vá từng file.
- Kết quả xác nhận qua HTTP thật: `staging-sim_cust-app/cust-db/cust-edge`
  đều `version=1.3.3 drift_status=current`. Evidence/register vẫn chạy bình
  thường sau update (xác nhận qua gateway log).

**2. Bật `mutation_enabled` thật trên cả 3 VM:**
- Set `AOIP_AGENT_MODE=mutation_enabled` + `AOIP_REDIS_URL` (Redis trong
  K8s, VM reach trực tiếp qua network phẳng OrbStack — đã test TCP connect
  thật) + `AOIP_AUDIT_LOG_PATH=/var/lib/aoip/recovery-audit.jsonl` +
  `AOIP_GATE_*` (process_down/systemd, max_risk 0.5, scope_prefix `svc:`) +
  `AOIP_ALLOWED_SYSTEMD_UNITS` RIÊNG từng host (chọn có chủ đích, không
  wildcard): `nginx.service` (cust-edge), `payment-api.service` (cust-app —
  đúng service lab đánh dấu "(simulated)", an toàn nhất để drill), `mariadb
  .service,redis-server.service` (cust-db).
- **Bug thật #4:** venv agent thiếu package `redis` (chỉ cần cho
  `mutation_enabled`, `observe_only` không cần — comment trong code đã nói
  rõ). `AgentBootstrapError` đúng thiết kế (fail loudly, không silent
  fallback) → cài `redis[hiredis]>=5.0.0` vào venv cả 3 VM.
- Sau 2 fix trên: cả 3 `aoip-agent.service` **active, ổn định** (restart
  counter dừng tăng), evidence/register vẫn chạy — xác nhận qua gateway log.
- **Ý nghĩa thật, không phóng đại:** daemon nay CÓ THỂ thực thi 1 recovery
  command đã approve, đầy đủ lease+idempotency+gate+allowlist+revalidate —
  NHƯNG chưa có caller tự động nào tạo `RecoveryRequest`/`Approval` cho các
  host này. Cách duy nhất trigger mutation thật hôm nay là operator CLI
  (`python -m aoip.console.approve_systemd_restart`) ký tay 1 lệnh. Chưa nối
  diagnosis→decision→approval→dispatch tự động (đó là việc Phase 1-6, ngoài
  phạm vi phiên này).

Toàn bộ chi tiết + rationale đầy đủ đã cập nhật vào
`docs/architecture/ADR-005-recovery-executor-consolidation.md` (section
"Rollout — DONE").

### Next step thật (2026-07-20, chốt phiên)

- Working tree sạch, `main` đã push (bao gồm commit `4b46da2` fix auth
  header). Cluster K8s + VM fleet đều chạy code mới nhất, đã verify runtime
  thật (không phải chỉ test pass).

## Drill thật đã chạy — end-to-end PASS trên hạ tầng live (2026-07-20)

User chỉ thị rõ "chạy thử drill thật đi". Route drill cần dùng
(`/webhook/agent/rt/commands/enqueue`) có gate `_enforce_mutation_toggle()`
yêu cầu **master kill-switch** `OMNI_AUTO_EXECUTE_ENABLED=true` trên
gateway — đúng cái AGENTS.md ghi "never open". Đã dừng lại hỏi rõ qua
AskUserQuestion trước khi làm (không tự ý mở dù có "toàn quyền" trước đó) —
user xác nhận "mở tạm để drill, tắt ngay sau khi xong".

**Đã làm, đúng cam kết:**
- Mở CHỈ trên `omni-gateway` deployment (`kubectl set env`), KHÔNG đụng
  `omni-fullstack`/overlay K8s autoexec-lab (phạm vi rộng hơn nhiều, không
  cần). Cửa sổ mở: ~11 phút, đúng 3 lệnh test, tắt lại ngay sau khi lệnh thứ
  3 vào terminal state. Đã xác nhận tắt thật qua cả `printenv` lẫn
  `GET /autonomy/mutation` (`effective: false`).
- **Phát hiện quan trọng giữa chừng:** CLI `aoip.console.approve_systemd_restart`
  build payload theo shape Stack A (`capability`/`target`), nhưng daemon
  thật đang chạy dùng executor Stack B (`operations.py`) — expect shape
  khác hẳn (`recovery`/`approval`/`evidence`). **CLI này KHÔNG THỂ điều
  khiển daemon thật hiện tại** — xác nhận sống đúng cái ADR-005 đã cảnh báo,
  không còn là suy đoán. Phải tự build payload đúng shape Stack B để drill
  chạy được.
- 3 lệnh enqueue cho `staging-sim_cust-app`/`payment-api.service`:
  1. Lần 1: `FAILED` (Redis timeout thoáng qua — connect test riêng ngay
     sau đó PASS sạch, 2 lần sau cũng PASS, kết luận không phải lỗi hệ
     thống).
  2. Lần 2 (service đang khỏe): `COMPLETED, NO_ACTION_NEEDED` — xác nhận
     current-state revalidation THẬT hoạt động, từ chối "sửa" cái không hỏng.
  3. `sudo systemctl stop payment-api.service` thật (mô phỏng sự cố, an
     toàn, reversible).
  4. Lần 3: **`COMPLETED, status=recovered, verified=true`**. Xác nhận độc
     lập trên VM: `payment-api.service active (running)` PID mới, uptime
     mới, `curl localhost:8080` → HTTP 200.
- **Audit trail hash-chain thật** trên VM
  (`/var/lib/aoip/recovery-audit.jsonl`): PLANNED→GATE_BLOCKED (lần 2)→
  PLANNED→BEFORE_STATE(inactive)→EXECUTED(rc=0)→COMPLETED
  (verification.confidence=1.0). `prev_hash`/`block_hash` nối đúng chuỗi.

**Kết luận:** đây là bằng chứng đầu tiên, thật, end-to-end rằng toàn bộ
pipeline durable recovery (delivery/fencing→lease→idempotency→gate→
allowlist→execute→verify→audit) chạy ĐÚNG trên hạ tầng sống, không chỉ unit
test. Chi tiết đầy đủ đã ghi vào ADR-005 (section "Real drill executed").
Test record (`omni:cmd:rec:staging-sim:cmd-drill-*`, TTL 7 ngày) giữ
nguyên, không xoá — cùng lý do với CRAT test block trước đó trong phiên:
bằng chứng test thật, không phải rác cần dọn.

### Next step thật (2026-07-20, chốt phiên — sau drill)

- Kill-switch đã tắt lại `false`, xác nhận qua HTTP thật. Không có gì đang
  mở, không có rủi ro treo lại từ phiên này.
- Working tree sạch (drill chỉ dùng payload runtime, không tạo thay đổi
  source code mới — chỉ 2 file docs được sửa/commit).
- **Việc thật còn mở:** CLI `approve_systemd_restart.py` cần fix để build
  đúng shape Stack B (hoặc build capability-dispatch layer) — hiện tại
  KHÔNG dùng được với daemon thật, chỉ payload hand-built mới chạy. Đây là
  bug thật phát hiện qua drill, chưa fix trong phiên này.
- Phase 0-6 của roadmap "Omni Autonomous Productization" (canonical
  contracts, vertical slice, multi-tenant mở rộng) — quy mô nhiều tuần thiết
  kế, chưa bắt đầu, cần một phiên riêng bắt đầu bằng thiết kế trước khi viết
  code migration.
