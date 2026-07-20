# ADR-005: Recovery Executor Consolidation (systemd_restart vs operations)

**Date:** 2026-07-20
**Status:** Accepted, option 2 implemented — user granted explicit go-ahead
for the recommended fix after reviewing this finding (see
`docs/handoffs/CURRENT_SESSION.md`, "P0-2"). Code + tests are in; **not yet
released to the VM fleet** (see "Rollout" section below — that is a separate
decision from writing the code).

## Context

Two independent recovery-executor factories exist for the `systemd.restart_unit`
capability, and they are **not** duplicates of the same thing — they are two
different halves of a pipeline that only partially recombine in production:

| | Build/validate (author-time) | Execute (runtime) |
|---|---|---|
| **Stack A** | `aoip/capabilities/systemd_restart.py::build_typed_payload` + `issue_capability_command`, invoked via `aoip/command_bridge.py::build_durable_command`, invoked via the operator CLI `aoip/console/approve_systemd_restart.py` | `aoip/capabilities/systemd_restart.py::build_systemd_restart_executor` |
| **Stack B** | `aoip/agent/operations.py::decode_recovery_command` (generic, no capability-specific typing) | `aoip/agent/operations.py::build_recovery_executor` |

Both executors ultimately call the **same** `operations.py::run_guarded_recovery`
(lease + `IdempotencyLedger` + `execute_recovery` current-state revalidation) —
that shared core is sound and not duplicated. The divergence is in what happens
**before** `run_guarded_recovery` is reached:

- Stack A's `build_systemd_restart_executor` (`src/aoip/capabilities/systemd_restart.py:337-420`)
  decodes via `_decode()`, which:
  - verifies `capability`/`capability_version` are supported (`describe_capability`);
  - verifies `approved_payload_hash` matches a hash of the typed fields — a
    tamper-binding between what was approved and what is executed
    (`systemd_restart.py:246-255`);
  - runs capability-specific preflight checks: `policy.is_allowed(unit)` against
    a configured `SystemdRestartPolicy` (env `AOIP_ALLOWED_SYSTEMD_UNITS`,
    `systemd_restart.py:120-134,371-377`) and `_unit_exists()` via a live
    `systemctl show` (`systemd_restart.py:379-387`).
- Stack B's `decode_recovery_command()` (`src/aoip/agent/operations.py:286-335`)
  only checks required-field presence (`_require`) and that
  `operator_for(failure_mode, substrate)` exists. **It does not call
  `validate_unit_name()`, does not check any unit allowlist, and does not bind
  a payload hash.** The only authority check that reaches this path is
  `RecoveryGate` inside `execute_recovery` (`src/aoip/recovery.py:224-231`):
  `failure_mode`/`substrate` membership, `risk <= max_risk`, and
  `req.failed_node.startswith(gate.scope_prefix)` — a coarse node-namespace
  prefix (e.g. `"svc:"`), not a specific unit allowlist. `req.unit` itself
  (the literal argv passed to `sudo systemctl restart {unit}` in
  `recovery.py::_sd_apply`, `recovery.py:65-67`) is never validated against
  any allowlist on this path.

**The live daemon wires Stack B, not Stack A.** Confirmed by direct read of
`src/aoip/agent/runtime_config.py:16,112-114`: `build_agent_runtime()` — the
function `daemon.py::main()` calls to construct the `mutation_enabled` executor
— imports and calls `operations.build_recovery_executor`. It never imports
`systemd_restart.build_systemd_restart_executor`, and never loads
`AOIP_ALLOWED_SYSTEMD_UNITS`/`SystemdRestartPolicy` at all. Per
`ADR-001-canonical-agent-runtime.md` (2026-07-13 update), `aoip.agent.daemon` is
the runtime actually deployed on all 3 VM lab hosts today (`aoip-agent.service`,
via `aoip.agent.employee`).

**Net effect:** the specific-unit allowlist and payload-hash tamper-binding that
`systemd_restart.py` was built to provide are real, tested
(`tests/test_capability_systemd_restart.py`), and reachable through the
operator CLI (`approve_systemd_restart.py` → `/webhook/agent/rt/commands/enqueue`)
— but a command built and enqueued through **any other path** that reaches the
same durable command channel (e.g., a future automated caller, or a
differently-authored payload matching Stack B's looser `_REQUIRED_RECOVERY_FIELDS`
contract) will execute with only the coarse `RecoveryGate` check, not the
per-unit allowlist. An operator who configured `AOIP_ALLOWED_SYSTEMD_UNITS`
believing it constrains what the live daemon can restart would be wrong — that
env var is never read by the live path.

This is a real, currently-live gap, not a hypothetical: `run_daemon()`'s
executor selection (`daemon.py:46-53`) is unconditional once
`redis`/`transport`/`audit_log`/`gate` are present — there is no capability
dispatch that would route a `systemd.restart_unit`-typed payload to
`build_systemd_restart_executor` instead.

## Options considered

1. **Wire `build_systemd_restart_executor` as the daemon's default executor**
   instead of `operations.build_recovery_executor`, for capabilities that have
   a typed builder. Pro: closes the gap directly, reuses tested code
   (`test_capability_systemd_restart.py`). Con: `build_systemd_restart_executor`
   is capability-specific (single `unit` target); a real dispatcher would need
   to route by `payload.get("capability")` to the right typed executor, falling
   back to `FAILED` (fail-closed) for unrecognized capabilities — this is new
   code, not a drop-in swap.

2. **Pull the allowlist + payload-hash checks up into `operations.py`'s generic
   path**, so every capability gets them uniformly instead of per-capability
   opt-in. Pro: one enforcement point, harder to bypass by adding a new
   capability that forgets the checks. Con: couples the generic recovery
   contract to systemd-specific policy shape (`SystemdRestartPolicy`); needs a
   generalized "target allowlist" concept in `RecoveryGate` instead.

3. **Do nothing; document and accept.** Rejected as a default — this is a real
   authority gap on the live production mutation path (any node under
   `gate.scope_prefix` × any unit name × correct failure_mode/substrate), not
   a cosmetic duplication.

## Recommendation (non-binding — requires explicit sign-off before implementing)

Option 2, generalized allowlist in `RecoveryGate`, is the more durable fix: it
removes the possibility of a *new* capability shipping without unit/target-level
authority checks, which option 1 does not guarantee by itself (a dispatcher
still requires every future capability to remember to build a typed executor).
Concretely: add an optional `allowed_targets: frozenset[str] | None` to
`RecoveryGate` (`src/aoip/recovery.py:100-107`), checked alongside
`scope_in_authority` in `execute_recovery`'s gate checks
(`src/aoip/recovery.py:224-231`) when non-None; populate it from
`AOIP_ALLOWED_SYSTEMD_UNITS` in `runtime_config.py::_gate_from_env` alongside
the existing `scope_prefix`/`allowed_failure_modes`/`allowed_substrates`
construction (`runtime_config.py:60-70`). This closes the gap for the live
daemon path without requiring a capability dispatcher, and Stack A's
`SystemdRestartPolicy`/`build_systemd_restart_executor` can then be evaluated
separately for whether its payload-hash tamper-binding should also move into
the generic contract (Phase 0 canonical-contract work) or stay specific to the
CLI-authored path.

## Implementation (2026-07-20, after sign-off)

Option 2 implemented as proposed:

- `RecoveryGate` (`src/aoip/recovery.py`) gained
  `allowed_targets: frozenset[str] = frozenset()` — same fail-closed
  convention as `SystemdRestartPolicy.allowed_units`: empty means restart
  nothing, never permit-all. `_gate_checks()` gained a `target_allowlisted`
  check (`req.unit in gate.allowed_targets`) alongside the existing
  `scope_in_authority` check — any gate failure is zero-mutation, same as
  every other check in that list.
- `runtime_config.py::_build_gate()` now reads `AOIP_ALLOWED_SYSTEMD_UNITS`
  via the same `_require()` helper used for every other gate field —
  `mutation_enabled` mode fails startup (`AgentBootstrapError`) if it's
  unset or blank, rather than silently building an unrestricted-by-omission
  gate. This matches the file's own stated philosophy ("MUTATION_ENABLED
  thiếu bất kỳ dependency nào phải làm startup THẤT BẠI").
- 11 pre-existing `RecoveryGate(...)` call sites (2 demo/CLI scripts,
  9 test files) updated to declare their unit(s) explicitly — they were all
  relying on the implicit "no restriction" default that no longer exists.
- New tests in `tests/test_aoip_runtime_config.py`: allowlist parses from
  env correctly, missing/blank env fails closed at startup, and a
  comma-only value (non-blank string, zero real entries) still parses to an
  empty — not permit-all — allowlist.

Full suite: `6211 passed, 5 deselected` after the change, zero regressions.

## Rollout — DONE (2026-07-20, explicit user instruction "bật multi agent lên chạy")

Originally deferred as a separate decision (see history below); the user
explicitly authorized both steps in the same session. Both are now live:

1. **Release 1.3.3 published and installed on all 3 lab VMs**
   (`staging-sim_cust-app/cust-db/cust-edge`, confirmed
   `drift_status=current` via `/webhook/agent/versions`). Required standing
   up lab TLS for the update channel (`INV_HTTPS_ONLY`) and fixing an
   unrelated pre-existing bug found along the way — the bundle download had
   no Authorization header and 401'd on any cluster with real API keys (see
   `fix(remote_agent): send Authorization header...` commit). Bootstrapped
   via one direct file sync (self-update cannot fix its own auth bug — the
   old code is what's trying and failing to download the fix); every update
   from 1.3.3 onward goes through the real channel.
2. **`AOIP_AGENT_MODE=mutation_enabled` set on all 3 VMs**, each with a
   real `RecoveryGate` (`AOIP_REDIS_URL` pointed at the in-cluster Redis,
   reachable directly from the VMs over OrbStack's flat network;
   `AOIP_AUDIT_LOG_PATH=/var/lib/aoip/recovery-audit.jsonl`) and a
   deliberately narrow `AOIP_ALLOWED_SYSTEMD_UNITS` per host: `nginx.service`
   (cust-edge), `payment-api.service` (cust-app — explicitly the
   "(simulated)" lab drill service), `mariadb.service,redis-server.service`
   (cust-db). All 3 daemons are stably `active` (`aoip-agent.service`
   restart counts stopped incrementing after the fix), telemetry/evidence
   flow confirmed unaffected post-change.

**What this does and does not mean:** the daemons can now execute an
*approved* recovery command end-to-end (lease + idempotency + gate +
allowlist + current-state revalidation, all live). It does **not** mean
anything mutates automatically — no live caller currently produces
`RecoveryRequest`/`Approval` payloads for these hosts; the only way to
trigger a real recovery today is a hand-authored, explicitly approved
command dispatched to `/webhook/agent/rt/commands/enqueue`. Wiring an
automated diagnosis→decision→approval→dispatch path remains out of scope
(that's the Phase 1-6 vertical-slice work, not this ADR).

**Correction found during the real drill below:** the sentence above
originally said "via the operator CLI
(`aoip.console.approve_systemd_restart`)". That CLI builds a **Stack A**
typed payload (`capability`/`target`/`reason` shape, via
`command_bridge.build_durable_command` →
`capabilities/systemd_restart.build_typed_payload`) — but the live daemon's
configured executor is **Stack B** (`operations.build_recovery_executor`),
whose `decode_recovery_command()` expects a completely different
`recovery`/`approval`/`evidence` shape. **The CLI tool cannot currently
drive the deployed daemon at all** — this is live confirmation of exactly
the mismatch this ADR describes, not a hypothetical. A hand-built
Stack-B-shaped payload was required for the drill to work. Fixing the CLI
to emit the shape the live executor actually consumes (or building the
capability-dispatch layer discussed in "Options considered" above) is
follow-up work, not done in this ADR.

## Real drill executed (2026-07-20) — end-to-end proof on live infrastructure

User explicitly authorized a full real drill, including opening the global
master kill-switch (`OMNI_AUTO_EXECUTE_ENABLED`) temporarily — a boundary
this session otherwise treats as absolute. Scope and safety:

- Opened **only** on `omni-gateway`'s own deployment env (`kubectl set env
  deployment/omni-gateway OMNI_AUTO_EXECUTE_ENABLED=true`) — not
  `omni-fullstack`, not the broader `omni-fullstack-autoexec-lab.yaml` K8s
  overlay (that overlay is for the unrelated K8s executor lane and changes
  far more than needed: `OMNI_AUTONOMY_TIER`, `OMNI_SIEM_SUGGEST_ONLY`,
  etc. — deliberately not touched).
- Window: ~11 minutes, 3 controlled test commands only, reverted
  immediately after the third command reached a terminal state — confirmed
  back to `false` via both `kubectl exec ... printenv` and
  `GET /autonomy/mutation` (`effective: false, reason:
  master_kill_switch_off`) before ending this step.
- Per-tenant flag `aoip_mutation_enabled` for `staging-sim` was already
  `true` from earlier work — not touched.

Three commands enqueued via `/webhook/agent/rt/commands/enqueue` for
`staging-sim_cust-app`, unit `payment-api.service` (the lab's explicitly
"(simulated)" drill service, port 8080, a plain `http.server`):

1. **First attempt**: `FAILED`, `executor_exception: Timeout connecting to
   server` (Redis). A direct connectivity check immediately after
   (`redis.asyncio` PING from the VM) succeeded cleanly — read as a
   transient cold-connection blip, not a systemic issue; the identical
   command shape succeeded on the next two attempts.
2. **Second attempt** (service still healthy): `COMPLETED`,
   `status=aborted, outcome=NO_ACTION_NEEDED, reason="service đang HEALTHY
   ngay trước execute — không tác động (zero mutation)"` — proves the
   current-state revalidation safety check is real: it refused to "fix" a
   service that wasn't actually broken, exactly as designed.
3. `sudo systemctl stop payment-api.service` on the VM (real, reversible,
   simulates an actual incident).
4. **Third attempt**: `COMPLETED`, `status=recovered,
   reason="service + dependents verified", verified: true`. Confirmed
   independently on the VM: `payment-api.service` `active (running)` with a
   fresh PID and start timestamp, `curl localhost:8080` → `HTTP 200`.

**Audit trail** (`/var/lib/aoip/recovery-audit.jsonl` on the VM, real
hash-chain, `prev_hash`/`block_hash` linked): `RECOVERY_PLANNED` →
`RECOVERY_GATE_BLOCKED` (attempt 2, `blocked: ["current_state_broken"]`) →
`RECOVERY_PLANNED` → `RECOVERY_BEFORE_STATE` (`before.active_state:
"inactive"`) → `RECOVERY_EXECUTED` (`verb: restart, rc: 0, approver:
claude-drill-2026-07-20`) → `RECOVERY_COMPLETED`
(`verification.confidence: 1.0`).

This is the first confirmed, real, end-to-end proof that the durable
recovery pipeline (delivery/fencing → lease → idempotency → gate →
allowlist → execute → verify → audit) works correctly against live
infrastructure, not just unit tests. Test artifacts left in place
(durable command records `omni:cmd:rec:staging-sim:cmd-drill-*`, 7-day TTL,
clearly ID'd as drill records) — not deleted, same reasoning as the P0-1
CRAT verification earlier this session: an immutable/durable record of a
real test is evidence, not noise to clean up.

<details>
<summary>Original deferred-rollout note (superseded above)</summary>

**This fix has zero live behavior impact today.** All 3 lab VMs
(`cust-edge`, `cust-app`, `cust-db`) run `aoip-agent.service` with
`AOIP_AGENT_MODE=observe_only` (confirmed via `orb -m <vm> sudo systemctl
show aoip-agent.service -p Environment`), which short-circuits before
`_build_gate()`/`RecoveryGate` are ever constructed
(`daemon.py`/`runtime_config.py:94-95`). The fix only takes effect once
BOTH: (a) a new agent release containing this code is published and
installed on a VM (via the IT-5 safe-update mechanism, `make
publish-agent-release` + the durable update/rollback channel — not a direct
file edit on the VM), AND (b) an operator deliberately sets
`AOIP_AGENT_MODE=mutation_enabled` with `AOIP_ALLOWED_SYSTEMD_UNITS` set —
itself already a distinct, gated decision this ADR does not touch.
Publishing a new agent release to the fleet is accordingly left as an
explicit follow-up, not bundled into this ADR.

</details>

## Consequences if left unresolved (pre-implementation baseline, for record)

- Any future automated caller of the durable command channel (not just the
  `approve_systemd_restart` CLI) that produces a Stack-B-shaped payload can
  restart any unit matching the coarse node-scope prefix, without the
  operator-configured unit allowlist applying.
- Operators configuring `AOIP_ALLOWED_SYSTEMD_UNITS` get a false sense of a
  restriction that the live daemon does not currently enforce.

## Verification performed for this ADR

Read-only: `src/aoip/agent/daemon.py`, `src/aoip/agent/operations.py`,
`src/aoip/agent/runtime_config.py`, `src/aoip/capabilities/systemd_restart.py`,
`src/aoip/recovery.py`, `src/aoip/command_bridge.py`,
`src/aoip/console/approve_systemd_restart.py`, `ADR-001-canonical-agent-runtime.md`.
Confirmed neither test suite exercised the *other* stack's executor before
the fix (`test_aoip_operations.py`'s `build_recovery_executor` tests never
asserted an allowlist check — the gap was structurally absent from that
path, not just untested). After implementation: full suite green, plus a
live check on the VM fleet that `AOIP_AGENT_MODE=observe_only` on all 3
hosts confirms this session's change carries zero live risk.
