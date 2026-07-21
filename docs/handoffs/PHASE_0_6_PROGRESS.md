# Phase 0–6 Progress Ledger

Source plan: see chat history 2026-07-20/21 session (canonical contracts →
automated closed-loop recovery). This file is the single source of truth for
what is actually DONE vs NOT_STARTED/BLOCKED — status here must match a real
command's output, never a description of intended work.

Status values: NOT_STARTED / IN_PROGRESS / BLOCKED / DONE.
DONE requires the Exit Criteria command to have actually run this session (or
a prior session, with output preserved below) — never inferred from code
existing or from a unit test alone when the phase's Exit Criteria calls for
more.

## Phase 0 — Fix confirmed defect + freeze canonical contracts

| Item | Status | Verification | Notes |
|---|---|---|---|
| 0a: fix CLI payload shape mismatch | DONE | see below | 2026-07-21, commits d987e9c/9db8a50/194f417 |
| 0b: canonical contracts in src/pkg/contracts/ | DONE | see below | 2026-07-21 |

### 0a verification (real, captured)

Fixes (3 commits): `issue_capability_command()` now emits a `"recovery"`
section the live decoder needs (`d987e9c`); `DeliveryLoop` now injects
`command_id` into the executor payload so correlation-scoped idempotency
works (`9db8a50`); `_restart_service()` unit name is now env-configurable —
the real fleet runs `aoip-agent.service`, not the hardcoded `omni-agent`
(`194f417`).

```
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
6219 passed, 5 deselected, 2 warnings   (was 6215 before Phase 0a; +4 new tests)
```

Real fleet deploy: published release 1.3.5 to all 3 VMs
(`staging-sim_cust-app/cust-db/cust-edge`), confirmed
`drift_status=current` via `/webhook/agent/versions`.

Real CLI drill (the actual Exit Criteria — not a hand-built payload):
```
python -m aoip.console.approve_systemd_restart --unit payment-api.service \
  --tenant staging-sim --agent-id staging-sim_cust-app ... \
  | curl -X POST .../webhook/agent/rt/commands/enqueue ...
```
→ `state: COMPLETED`, `outcome: {"status": "recovered", "reason": "service +
dependents verified", "evidence": ["before=inactive", "service_health=ok",
"dependents=n/a"], "verified": true}`. Confirmed independently on the VM:
`payment-api.service active (running)`, fresh PID, `curl localhost:8080` →
`HTTP 200`.

Kill-switch (`OMNI_AUTO_EXECUTE_ENABLED`) opened only on `omni-gateway`, only
for this verification window, confirmed reverted to `false` before moving
on (`GET /autonomy/mutation` → `effective: false`).

Self-update mechanism note (not blocking, logged for later): the official
self-update channel (UPDATE_AGENT command) still has a live, separate,
timing-related gotcha — the daemon restarting itself mid-execution can get
killed by its own `systemctl restart` before reporting a result. Worked
around this session via direct file sync + external restart (same pattern
used successfully earlier this session). Root-causing the self-restart race
is not in scope for Phase 0a; flagged for Phase 1 or a dedicated follow-up.

### 0b verification (real, captured)

New module `src/pkg/contracts/` (identity.py, evidence.py) — pure
dataclasses, stdlib-only, additive (no existing call site wired to it yet —
that is explicitly out of scope per the module's own docstring; the K8s and
VM Evidence shapes are semantically close enough that forcing every call
site onto the canonical shape immediately would be a bigger, riskier change
than "freeze the contract and prove it's lossless").

Scope actually delivered vs. the phase's original framing: Evidence
unification is real and tested. Command/CommandResult unification is
narrowed to `CorrelationIdentity` (the 6 shapes' shared identity fields) —
the six shapes' transport envelopes themselves are NOT unified here; that is
Phase 3's "pick one canonical capability-dispatch path," not this phase.
Recorded here instead of silently expanding scope to match the original
one-line description.

```
.venv/bin/python -m pytest tests/test_pkg_contracts.py -q
6 passed

.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
6225 passed, 5 deselected, 2 warnings   (was 6219 after Phase 0a; +6 new tests)
```

Deploy + smoke: `make deploy-worker docker-gateway deploy-gateway` — both
pods Running; `/healthz`/`/readyz` green; confirmed the module actually
imports and runs INSIDE the live `omni-fullstack` pod (not just "image
built"):
```
kubectl exec -n multi-agent deploy/omni-fullstack -- python3 -c "
from pkg.contracts.evidence import from_diagnostic_evidence_dict
from pkg.contracts.identity import CorrelationIdentity
e = from_diagnostic_evidence_dict({'trace_id':'t','probe':'p','result':'FAILED'})
print('in-pod import+use OK:', e.trace_id, e.probe, e.result)
"
in-pod import+use OK: t p FAILED
```
Zero runtime behavior change intended or observed — no existing pipeline
touches this module yet.

## Phase 1 — Vertical slice E2E script (no hand-authored JSON)

| Item | Status | Verification | Notes |
|---|---|---|---|
| scripts/e2e_recovery_drill.py | DONE | see below | 2026-07-21 |

### Phase 1 verification (real, captured)

`scripts/e2e_recovery_drill.py` — non-destructive, real CLI
(`aoip.console.approve_systemd_restart`, not hand-built JSON), against real
`staging-sim_cust-app`/`payment-api.service`. Manages its own
`kubectl port-forward` and restarts it after every kill-switch flip (a real
bug found running this the first time: `kubectl set env` on omni-gateway
triggers a pod rollout, which silently kills any existing port-forward
tunnel — first run failed with `Connection refused` on the teardown check
before this was fixed).

Exit Criteria run (verbatim, 2 consecutive runs as required):
```
.venv/bin/python scripts/e2e_recovery_drill.py --runs 2
--- run 1/2 (run_id=5ef6da21) ---
[PASS] {"run_id": "5ef6da21", "pre_stop_active": false,
  "command_id": "cmd-f2b0f4c838134d24", "state": "COMPLETED",
  "outcome": {"status": "recovered", "reason": "service + dependents verified",
    "evidence": ["before=inactive", "service_health=ok", "dependents=n/a"],
    "rc": 0, "verified": true},
  "service_http_code": "200", "audit_block_found": true}
--- run 2/2 (run_id=9277d838) ---
[PASS] {"run_id": "9277d838", ... same shape, command_id=cmd-900f2be608dc48db,
  "service_http_code": "200", "audit_block_found": true}
[TEARDOWN] kill-switch reverted, effective mutation state:
  {'tenant_id': 'staging-sim', 'requested': True, 'master_kill_switch': False,
   'effective': False, 'reason': 'master_kill_switch_off'}
=== 2 passed, 0 failed (of 2) ===
```
Post-run confirmed independently: `OMNI_AUTO_EXECUTE_ENABLED=false` on the
live gateway pod, `omni-gateway` pod Running, `payment-api.service` active
(correct end state — recovered, left running).

Full suite unaffected: `6225 passed, 5 deselected` (unchanged from Phase 0b
— this script is not a pytest test, it's a standalone E2E runner).

## Phase 2 — tier_gate parity for VM mutation lane

| Item | Status | Verification | Notes |
|---|---|---|---|
| wire resolve_tier into _enforce_mutation_toggle/run_guarded_recovery | DONE | see below | 2026-07-21 |

### Phase 2 verification (real, captured)

**Move + wire:** `pkg.autonomy.tier_gate` (new — moved from `workers/tier_gate.py`,
same pattern as `pkg.risk_taxonomy`/`workers/risk_class.py`, since gateway
cannot import `workers/`). `workers/tier_gate.py` is now a re-export shim,
transparent to existing K8s callers (37/37 `test_tier_gate_and_hitl.py`
unchanged). `agent_runtime.py::_enforce_mutation_toggle` now also calls
`_enforce_tier_gate()` — same `resolve_tier()`/`gate_decision_for_tool()`
the K8s lane uses, keyed on the payload's declared `capability` (present on
every payload since Phase 0a's fix). Added `"systemd.restart_unit": LOW` to
`pkg/risk_taxonomy.py` (direct VM-lane counterpart of the existing
`k8s_rollout_restart: LOW` entry) — without it, every VM recovery command
would fail-closed to HIGH risk (unknown capability) and require HITL at
every tier, not the intended K8s parity.

**Second real bug found live** (not introduced this phase, pre-existing):
`GET /autonomy/tier` read `repo.get_tier(tenant_id) or "shadow"` directly —
bypassing both the Redis cache AND the env-derived fallback `resolve_tier()`
uses. When PG has no explicit row for a tenant (common), an operator would
see "shadow" while the REAL gate (now used by both K8s and, as of this
phase, VM recovery) could be resolving "auto" from the legacy
`OMNI_AUTO_EXECUTE_ENABLED` env var — the displayed and effective tiers
diverged. Caught live: `GET /autonomy/tier?tenant_id=staging-sim` returned
`"shadow"` immediately before a drill whose actual effective tier (kill-switch
open, no PG row) was `"auto"`. Fixed: `GET /tier` now calls the same
`resolve_tier()` the gate uses.

```
.venv/bin/python -m pytest tests/test_gateway_agent_runtime.py tests/test_autonomy_tier_endpoint.py tests/test_tier_gate_and_hitl.py -q
39 passed

.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
6230 passed, 5 deselected, 2 warnings   (was 6225 after Phase 1; +5 new tests)
```

**Real Exit Criteria proof** (both halves, live, via `POST /autonomy/tier`
+ `scripts/e2e_recovery_drill.py`, real gateway/PG/Redis, tenant `staging-sim`):
- Set tier=`shadow` explicitly → ran drill →
  `HTTP 423 {"reason": "tier_gate_suggest", "tier": "shadow", "risk_class": "LOW"}`
  — blocked, exactly as the tier×risk matrix specifies (shadow always SUGGEST
  for non-READONLY risk). Confirmed the drill's own enqueue call failed
  closed, not just a unit-test mock.
- Set tier=`auto` (confirm=true) → discovered live that a pre-existing
  **provider plan ceiling caps `staging-sim` at `assist`** (matches a
  2026-07-15 session note: "tenant_plan ceilings are assist for all three
  tenants" — `_apply_plan_ceiling()` working as designed, not a bug) → ran
  drill → `COMPLETED status=recovered verified=true` — assist tier + LOW risk
  = ALLOW, correctly.
- Reverted tier back to `shadow` (its original ambient value) after the test;
  confirmed via `GET /autonomy/tier` → `{"tier": "shadow"}`.

Post-verification confirmed: `OMNI_AUTO_EXECUTE_ENABLED=false` on the live
gateway pod, both pods Running, `payment-api.service active`.

## Phase 3 — Remaining isolation/architecture gaps

| Item | Status | Verification | Notes |
|---|---|---|---|
| unify Stack A/Stack B capability dispatch (payload-hash tamper-binding) | DONE | see below | 2026-07-21 |
| durable PG-backed agent identity (TTL-squatting fix) | DONE | see below | 2026-07-21 |

### Phase 3a verification (payload-hash unification, real, captured)

Moved `capability_payload_hash()` out of `capabilities/systemd_restart.py`
(Stack A, never live-wired) into `agent/operations.py` (Stack B, the actual
deployed executor) — `systemd_restart.py` now imports it back (no circular
import: it already imports `run_guarded_recovery` from `operations.py`, so
the shared definition must live there). `decode_recovery_command()` now
verifies `approved_payload_hash` whenever `payload["capability"]` is
present — the live daemon gained real tamper-binding it never had.

**Real bug found deploying this** (not hypothetical): the FIRST live drill
after deploying the hash check failed with `payload_hash_mismatch` on a
genuinely untampered payload from the real CLI — a 100% false positive that
would have broken every real recovery command. Root cause:
`reason.diagnosed_at` (a full-precision Unix timestamp float) does not
reliably round-trip to a byte-identical JSON string across the real
transport chain (CLI's `json.dumps` → gateway's Pydantic/pydantic-core →
Redis storage → VM daemon's httpx) even though the underlying float value is
unchanged. Fixed: `capability_payload_hash()` strips `reason.diagnosed_at`
before hashing (a freshness-metadata field, not an identity field — the
security-relevant fields — `capability`, `target.unit`,
`reason.mission_id/decision_id/incident_id/summary` — remain hashed and
protected; `approval.issued_at/expires_at` bound the freshness separately
and are not part of this hash's input at all).

```
.venv/bin/python -m pytest tests/test_capability_systemd_restart.py tests/test_aoip_operations.py tests/test_aoip_command_bridge.py tests/test_m1_systemd_recovery_e2e.py tests/test_aoip_console.py -q
88 passed

.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
6233 passed, 5 deselected, 2 warnings   (was 6230 after Phase 2; +3 net after both the hash-check tests and the diagnosed_at-jitter regression test)
```

Published release 1.3.7 (includes both the hash-binding fix and the
diagnosed_at fix) to all 3 VMs, `drift_status=current` confirmed. Real drill
via `scripts/e2e_recovery_drill.py --runs 2`: **2/2 passed** with the hash
check active end-to-end — `COMPLETED status=recovered verified=true` both
runs, no false positives. Kill-switch + tier reverted to their pre-test
state (`shadow`) after.

### Phase 3b verification (durable agent identity, real, captured)

**Scope correction found during investigation**: the original framing
("registry TTL-expiry squatting") undersold how much was already fixed —
per-agent credentials (IT-3, this session's earlier P0-4 fix) already
protect any agent using one, since `ctx.agent_id` is checked before the
registry is even consulted. Checked the real fleet: **2 of 3 real VMs
(cust-edge, cust-db) still authenticate with the tenant-SHARED key, not a
per-agent credential** (`cust-app` is the only one migrated) — so this gap
is live on real infrastructure today, not hypothetical.

New: `migrations/omni_admin/0010_agent_identity_claim.sql` — durable, no-TTL
`(agent_id PRIMARY KEY, tenant_id, first_claimed_at)` table.
`AdminConfigRepo.get_or_claim_agent_owner()` — atomic
`INSERT ... ON CONFLICT (agent_id) DO NOTHING RETURNING tenant_id`, single
round-trip, race-safe. `require_agent_tenant()` now takes an optional
`repo` param — when the Redis registry has no live record, consults this
durable table instead of falling open to "first claim always wins"; when
`repo=None` (lightweight harnesses), behavior is unchanged (backward
compatible — confirmed by the full suite passing with zero test
modifications needed at the 13 existing call sites).

```
.venv/bin/python -m pytest tests/test_agent_identity_claim.py -q
8 passed

.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
6241 passed, 5 deselected, 2 warnings   (was 6233; +8 new tests, zero existing
tests modified — the repo=None fallback path)
```

**Real live proof against real PG** (not just unit tests — the Exit
Criteria explicitly asked for this): registered a disposable agent_id under
the real `staging-sim` tenant key → confirmed a durable claim row landed in
`omni_admin.agent_identity_claim` on the real Postgres pod → deleted the
Redis registry key directly (`redis-cli DEL`, simulating the 120s TTL
having elapsed) → attempted registration of the **same agent_id** using the
real `tenant-replay-01` tenant key → **`HTTP 403 {"detail": "agent_id is
registered to a different tenant"}`** — blocked, exactly as designed (before
this fix, this would have silently succeeded as a "first claim"). Confirmed
the original owner (`staging-sim`) can still re-register normally
afterward. Test data cleaned up (PG row deleted, Redis key already gone).

## Phase 4 — Automated diagnosis→decision→approval→dispatch

| Item | Status | Verification | Notes |
|---|---|---|---|
| wire diagnosis_loop → command_bridge.build_durable_command | DONE | real live drill, see below | zero manually authored JSON |

**What was built:** `services/analyst/diagnosis_loop.py` gained an optional
`suggested_recovery` field in its output schema (`{"capability":
"systemd.restart_unit", "unit": "..."}` or `null`), populated only when
diagnosis_complete=true, confidence high, and the unit name is grounded
(verbatim in the evidence facts or a command output this session — validated
by `_apply_grounding_gate`, which strips an ungrounded unit to `null`).
`workers/auto_recovery_bridge.py` (new) turns that into a
`command_bridge.build_durable_command()` call and POSTs it to the gateway's
own `/webhook/agent/rt/commands/enqueue` over real HTTP (worker → gateway are
separate pods) — fail-closed at every step (no suggestion / low confidence /
`OMNI_GATEWAY_API_KEY` unset → skip, logged, never raises). Wired into
`remote_agent_pipeline.py::_run_diagnosis_and_notify()` after CRAT+Telegram
succeed (best-effort last hop). The gateway's own Phase 2 tier_gate still
applies to every dispatch — this module does not and cannot bypass it.

**Two real bugs found and fixed live** (not hypothetical — both blocked the
first drill attempts): (1) `OMNI_AGENT_SERVICES_ENABLED` was off in
cust-app's `run.env`, so the systemd-failure collector never ran on that
host at all — enabled it. (2) `collectors/services.py` and `discovery.py`
both used `unit_full.rstrip(".service")` — `.rstrip()` strips a *character
set*, not a literal suffix, so `"payment-api.service"` lost its trailing `i`
too and became `"payment-ap"`, feeding a wrong unit name into the diagnosis
LLM (which then correctly-but-uselessly concluded the service was
"missing"). Fixed both to `.removesuffix()` (matching the already-correct
pattern in `collectors/discovery_evidence.py`), added regression tests,
published agent v1.3.8, deployed to cust-app.

**Real live drill — full closed loop, zero manually authored JSON:**
1. Crash-looped `payment-api.service` on cust-app (`systemctl kill -s
   SIGKILL` × N to force a genuine `failed` state — a clean `systemctl stop`
   leaves it `inactive`, which the collector does NOT treat as a failure;
   only `failed`/`activating` states are collected — a real, narrow
   detection-scope finding worth documenting for Phase 6).
2. Opened `OMNI_AUTO_EXECUTE_ENABLED=true` on `omni-gateway` only; confirmed
   `staging-sim`'s tenant mutation toggle already `true` in PG.
3. Natural evidence cycle (20s collector interval) detected the failure,
   `remote_agent_pipeline` launched `diagnosis_loop` with **zero manual
   intervention**: `trace_id=ra-c16cbcc78691 agent_id=staging-sim_cust-app
   probe=service_systemd_units`.
4. Diagnosis completed for real: `confidence=0.95`, `root_cause="The
   payment-api service is crashing repeatedly, causing systemd to kill it
   with a SIGKILL signal."`, `suggested_recovery={"capability":
   "systemd.restart_unit", "unit": "payment-api"}`.
5. `staging-sim`'s tier was found to be explicitly `shadow` in PG (a real
   governance setting from earlier Phase 3 cleanup today, not a bug) — SUGGEST-only
   at that tier, so the first dispatch attempt correctly got `HTTP 423`
   from the gateway's tier_gate. Temporarily raised to `auto` via the same
   `AdminConfigRepo.set_tier()` the real Admin API uses (plan-ceiling capped
   it to `assist`, which still ALLOWs LOW risk) — same narrow-window,
   revert-after discipline as the kill-switch, actor tagged
   `phase4-e2e-drill-temp`/`-revert`.
6. Real trace stages captured (`omni:trace:stages:ra-c16cbcc78691`):
   `EVIDENCE ok → LLM ok → SCHEMA ok → CRAT ok (audit block written) →
   DISPATCH ok (Telegram) → AUTO_RECOVERY ok (reason=dispatched
   command_id=cmd-a8b1cfc8040448aa state=QUEUED)`.
7. Real command terminal record
   (`omni:cmd:rec:staging-sim:cmd-a8b1cfc8040448aa`): `state=COMPLETED`,
   `outcome={"status":"recovered","reason":"service + dependents
   verified","evidence":["before=failed","service_health=ok",
   "dependents=n/a"],"rc":0,"verified":true}`,
   `approval.approver="auto-recovery:diagnosis_loop"` (explicitly marks it
   system-generated, not human), full typed payload (capability/target/
   preconditions/verification/recovery/evidence) built entirely by
   `build_durable_command()` — no hand-authored JSON anywhere in this chain.
8. **`orb -m cust-app systemctl is-active payment-api.service` → `active`**
   — the VM service genuinely recovered.

**Cleanup after the drill (all confirmed):** `staging-sim` tier reverted to
`shadow` (`AdminConfigRepo.set_tier(tier='shadow', actor='phase4-e2e-drill-revert')`,
cache cleared); `OMNI_AUTO_EXECUTE_ENABLED` reverted to `false` on
`omni-gateway` (confirmed via `printenv` on the live pod); the temporary
`Restart=no` drop-in used for one intermediate test iteration was removed
and the unit's original `Restart=always` config restored;
`payment-api.service` confirmed `active`.

**Known limitation, not fixed this session:** the systemd-failure collector
only observes `failed`/`activating` states (`systemctl list-units
--state=failed,activating`), so a clean `systemctl stop` (or any graceful
shutdown that doesn't hit systemd's restart rate-limit) is invisible to the
diagnostic pipeline — indistinguishable from an intentional stop by design,
but it also means a single non-crash-looping failure with `Restart=no`-style
units needs to reach `failed` state to be observed at all, which it does
immediately (no rate-limit needed) — only `Restart=always`-style units need
several rapid failures to *reach* `failed` in the first place, and by the
time they do, the failure legitimately looks like a crash-loop to the
diagnosis LLM (which correctly, conservatively declines to auto-suggest a
"plain restart is sufficient" for what looks like a recurring code bug —
seen live: 4 of 5 real diagnosis sessions against a genuine crash-loop
declined `suggested_recovery`, and that's arguably the *correct* behavior,
not a bug to fix). The one clean, unambiguous single-failure test (achieved
here via a temporary `Restart=no` override) is what got a populated
`suggested_recovery` on the first attempt every time.

## Phase 5 — Multi-tenant/multi-agent concurrency proof

| Item | Status | Verification | Notes |
|---|---|---|---|
| concurrent real drills, 2 tenants | DONE | see below | 2026-07-21 |

### Phase 5 verification (real, captured)

**Setup.** Only 3 real VMs exist in the lab (`cust-edge`/`cust-app`/`cust-db`, all
running `staging-sim` agents). `tenant-replay-01` is a real, already-provisioned
tenant (row in `omni_admin.tenant`, real key in the gateway's `OMNI_TENANT_APIKEYS`
secret) with no live VM daemon of its own. To get a genuine second *live* agent
process for a second tenant without provisioning new infra, `cust-edge` was
temporarily re-identified: stopped `aoip-agent.service`, backed up `run.env`,
rewrote it to `OMNI_AGENT_ID=tenant-replay-01_cust-edge` /
`OMNI_AGENT_TENANT_ID=tenant-replay-01` / `OMNI_AGENT_API_KEY=<tenant-replay-01's
real gateway key>` (target unit unchanged: `nginx.service`, already the VM's real
service — no re-scoping needed), restarted. Confirmed evidence flowing under the
new identity via `omni:remote_agent:registry:tenant-replay-01_cust-edge`
`last_seen` within 5s of restart, no 401/403 in `journalctl`. `staging-sim`
continued using `cust-app`/`payment-api.service` (same target as Phase 4).
Narrow-window elevation for the drill (same pattern as Phase 4, via the real
`/autonomy/mutation` + `/autonomy/tier` Admin API, not hand-edited PG rows):
`tenant-replay-01` mutation toggle → `true`; both tenants' tier → `auto`
(confirmed capped to `assist` by the `standard`-plan ceiling, same as Phase 4);
Redis tier cache (`omni:cfg:tier:{tenant}`) `DEL`'d after each `set_tier` call
(same known write-through gap as Phase 4); kill-switch opened on `omni-gateway`
only, confirmed via `printenv` after `set env`.

**Concurrent trigger.** `payment-api.service` (temporary `Restart=no` override,
same as Phase 4 — makes a single `SIGKILL` land in a clean, unambiguous `failed`
state instead of a self-healing loop) and `nginx.service` (already `Restart=no`
by default) were `SIGKILL`'d in the same shell command
(`... kill ... & ... kill ... & wait`) — both VMs reached `failed` within the
same second.

**Real concurrent processing observed** (`kubectl logs deploy/omni-fullstack`,
timestamps are Unix epoch seconds):
```
1784604815.27  [diag-loop] START trace=ra-b79b97f5a146 agent=tenant-replay-01_cust-edge
1784604819.85  [diag-loop] START trace=ra-787d2614c23e agent=staging-sim_cust-app
1784604837.93  [diag-loop] START trace=ra-c9d015160d55 agent=tenant-replay-01_cust-edge
1784604842.54  [diag-loop] START trace=ra-59e51a5336a7 agent=staging-sim_cust-app
```
Two tenants' diagnosis sessions genuinely interleaved on the same single
`omni-fullstack` replica (1/1) — not sequential batches.

**Outcome — both tenants recovered end-to-end via the real automated path,**
captured directly from the durable command records in Redis:

- `staging-sim`: trace `ra-59e51a5336a7` → `omni:cmd:rec:staging-sim:cmd-f529db38f04a4fda`
  — `tenant_id=staging-sim`, `canonical_scope=staging-sim:svc:payment-api.service`,
  `state=COMPLETED`, `outcome={"status":"recovered","verified":true}`.
- `tenant-replay-01`: trace `ra-c0af42aaad8c` (its 5th diagnosis attempt on this
  incident — the LLM declined `suggested_recovery` on 4 earlier, unambiguous
  single-failure attempts for `nginx`, a non-determinism the Phase 4 ledger already
  documented for `payment-api`; not something this phase changes) →
  `omni:cmd:rec:tenant-replay-01:cmd-1b5a042e2fcb47d2` — `tenant_id=tenant-replay-01`,
  `canonical_scope=tenant-replay-01:svc:nginx.service`, `state=COMPLETED`,
  `outcome={"status":"recovered","verified":true}`.

Both records confirmed live via `systemctl is-active` on their respective VMs
(→ `active`). Distinct `command_id`, `trace_id`/`incident_id`, `mission_id`,
`decision_id`, `fencing_token`, and `canonical_scope` for every field checked —
no value from one tenant's record appears in the other's.

**Isolation mechanism audit (live-verified, not just read):**
- **Idempotency** (`aoip/agent/idempotency.py::command_identity()`): key = hash of
  `tenant + mission_id + incident_id + decision_id + action_id + command_id +
  payload_hash` — tenant is part of the hash input, so two tenants can never
  collide on this key even with identical unit names/payloads. Confirmed by
  inspection of both real records above (different `tenant_id` on every level).
- **Command/audit layer** (`aoip.agent.runtime.EnqueueRuntimeCommand` /
  `CommandRecord`): `canonical_scope` is `"{tenant}:svc:{unit}"` — tenant-prefixed
  at this layer, confirmed directly from both real records
  (`staging-sim:svc:payment-api.service` vs. `tenant-replay-01:svc:nginx.service`).
- **VM-side `ExecutionLease`** (`aoip/agent/lease.py`, used inside
  `run_guarded_recovery` as the single-writer lock on `req.failed_node`, i.e.
  `svc:{unit}` — **bare unit name, NOT tenant-prefixed**): this drill's two real
  units (`payment-api.service` / `nginx.service`) never collided naturally, so a
  live VM drill could not exercise the same-name case. Verified it directly
  against the real shared Redis instead (`kubectl exec deploy/omni-fullstack`,
  `aoip.agent.lease.ExecutionLease`, real `redis.multi-agent.svc.cluster.local`):
  `lease.acquire("svc:collision-test-unit.service", holder="staging-sim-holder")`
  → token; a second `acquire()` for the same scope with
  `holder="tenant-replay-01-holder"` → `None` (denied), `holder_token()` still
  shows the first holder. **Residual finding, not fixed this phase**: the lease
  key itself has no tenant namespace, so two tenants with an identically-named
  allowed unit would contend for the same Redis lock. Confirmed this is
  fail-safe, not fail-open: a denied `acquire()` returns `None` →
  `run_guarded_recovery` aborts with zero mutation
  (`aoip/agent/operations.py` lease-denied branch), and even if it weren't
  denied, each VM's `transport` (`LocalTransport`/`SSHTransport`) is bound to
  that specific agent process at startup from its own env — a lease held by
  tenant A can never cause tenant B's daemon to execute anything, since there is
  no code path from "won a lease" to "mutate a different agent's host." Net
  effect of the gap: possible false contention (one tenant's legitimate op
  denied because of a same-named unit on another tenant), never cross-tenant
  mutation. Recommended for future hardening (not done here, out of this
  narrow phase's scope): prefix the lease scope with tenant, e.g.
  `f"{tenant}:svc:{unit}"`, matching the pattern the command/audit layer
  already uses.

**Unrelated observation surfaced mid-drill (not a Phase 5 defect):** the
Telegram-delivered advisory for trace `ra-787d2614c23e` came from the
`SYS_RESOURCE` (3σ resource-anomaly) lane, not the `SYS_HARD_FAIL`
(systemd-units collector) lane Phase 4 wired auto-recovery into — it reacted to
the crash's side effects with generic `ps`/`free` commands and correctly stayed
advisory-only (no `suggested_recovery` field exists in that lane's schema). The
lane that *is* wired (`SYS_HARD_FAIL`) ran independently on the same incident and
recovered it for real (`cmd-f529db38f04a4fda` above). Two lanes producing two
separate advisories for one underlying incident is existing, pre-Phase-4
behavior — out of scope to change here, noted for Phase 6 documentation.

**Cleanup after the drill (all confirmed, not assumed):** both tenants' tier
reverted to `shadow` via `/autonomy/tier` (real API call, `dedup_key` returned);
both mutation toggles reverted to `false` via `/autonomy/mutation`; Redis tier
cache `DEL`'d again; kill-switch reverted to `false` on `omni-gateway`, confirmed
via `printenv` on the live pod; `cust-edge` stopped, `run.env` restored from
backup (`OMNI_AGENT_ID=staging-sim_cust-edge` confirmed), agent restarted,
evidence flowing again under the original identity confirmed
(`omni:remote_agent:registry:staging-sim_cust-edge` `last_seen` within 17s of
restart); `payment-api.service`'s temporary `Restart=no` override directory
removed, `daemon-reload`d, `Restart=always` confirmed restored; both
`payment-api.service` and `nginx.service` confirmed `active` on their VMs;
collision-test lease key deleted from Redis; `git status` clean.

## Phase 6 — Convergence proof + docs close-out

| Item | Status | Verification | Notes |
|---|---|---|---|
| fresh grep audit, before/after shape counts | DONE | see below | 2026-07-21 |

### Phase 6 verification (real, captured)

Re-ran the same grep-based audit used to size the original plan. Full detail +
exact grep commands in the new `docs/architecture/ADR-006-evidence-command-
contract-convergence.md`. Honest result, no false unification claim:

- **Evidence shapes: still 3, unchanged** at their original call sites
  (`DiagnosticEvidenceDict`, `EvidenceItem`/`AgentEvidenceRequest`,
  `EvidenceObject`). Phase 0b's canonical `CanonicalEvidence`
  (`src/pkg/contracts/evidence.py`) exists and is proven lossless but has
  **zero production import sites** — confirmed via
  `grep -rln "pkg.contracts.evidence" src/ tests/` → only
  `tests/test_pkg_contracts.py`.
- **Command/CommandResult shapes: still 6, unchanged** (`ToolCallPayload`,
  `CommandItem`/`CommandResultItem`, `EnqueueRuntimeCommand`, `CommandRecord`,
  `RecoveryRequest`/`decode_recovery_command`, `issue_capability_command`).
  `CorrelationIdentity` has the same zero-adoption status.
- This is not a regression — Phase 0b explicitly scoped itself as "freeze the
  contract and prove it's lossless," not "migrate every call site." Nothing in
  Phases 1–5 routed through the canonical module. Recorded honestly rather
  than silently declaring convergence the plan didn't actually deliver.

**What DID converge (real, live-verified across Phases 2/4/5, not just
read):** the governance/decision layer — one shared `tier_gate` authority
gating both lanes (Phase 2), one shared `risk_taxonomy` table, the VM lane
gained the same diagnosis→confidence-gated→auto-dispatch shape the K8s lane
already had (Phase 4), and multi-tenant isolation at the identity/audit layer
is provably uniform across both lanes under real concurrency (Phase 5, zero
cross-tenant leakage). Both lanes already shared the CRAT audit ledger
pre-dating this roadmap.

**Decision (recorded in ADR-006, not just this ledger):** do not force
wire-shape migration retroactively — the governance-layer convergence is what
made the two lanes "one system" in the sense this roadmap's audit originally
cared about (uniform tier/risk/audit regardless of which lane a mutation came
through). Wire-shape unification remains available future work
(`src/pkg/contracts/` is ready) but touches every evidence/command call site
in both lanes — deferred until a concrete pain point makes that cost worth
paying.

**Docs updated:** `docs/architecture/ADR-006-evidence-command-contract-
convergence.md` (new — full audit + decision record);
`docs/CODEBASE.md`'s "Current verified state" section (new paragraph pointing
to ADR-006, replacing the implicit "two separate systems" framing with the
accurate governance-converged / wire-shapes-separate-by-design state).

```
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
6269 passed, 5 deselected, 2 warnings   (unchanged — docs-only phase)
```

## Roadmap status: Phase 0–6 all DONE

Every phase's Exit Criteria ran for real this session (or a prior session in
this same continuous effort, with output preserved above) — no phase was
marked DONE on the strength of code existing or unit tests alone where the
Exit Criteria called for a live drill. The two most consequential live proofs:
Phase 4 (a real VM incident detected, diagnosed, and recovered with zero
manually authored JSON) and Phase 5 (the same loop run concurrently across two
real tenants with zero cross-tenant leakage, plus one residual gap found and
honestly documented rather than hidden). Phase 6 closes the loop with an
honest convergence audit rather than a claimed one.
