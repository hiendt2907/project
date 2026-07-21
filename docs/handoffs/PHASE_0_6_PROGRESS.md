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
| wire resolve_tier into _enforce_mutation_toggle/run_guarded_recovery | NOT_STARTED | — | |

## Phase 3 — Remaining isolation/architecture gaps

| Item | Status | Verification | Notes |
|---|---|---|---|
| unify Stack A/Stack B capability dispatch | NOT_STARTED | — | |
| durable PG-backed agent identity (TTL-squatting fix) | NOT_STARTED | — | |

## Phase 4 — Automated diagnosis→decision→approval→dispatch

| Item | Status | Verification | Notes |
|---|---|---|---|
| wire diagnosis_loop → command_bridge.build_durable_command | NOT_STARTED | — | the actual closed-loop proof |

## Phase 5 — Multi-tenant/multi-agent concurrency proof

| Item | Status | Verification | Notes |
|---|---|---|---|
| concurrent real drills, 2 tenants | NOT_STARTED | — | |

## Phase 6 — Convergence proof + docs close-out

| Item | Status | Verification | Notes |
|---|---|---|---|
| fresh grep audit, before/after shape counts | NOT_STARTED | — | |
