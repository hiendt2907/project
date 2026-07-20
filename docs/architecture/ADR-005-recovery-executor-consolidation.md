# ADR-005: Recovery Executor Consolidation (systemd_restart vs operations)

**Date:** 2026-07-20
**Status:** Proposed — decision needed before wiring more capabilities onto the
durable agent daemon. No code changed by this ADR; it documents a finding from
this session's audit (see `docs/handoffs/CURRENT_SESSION.md`, "P0-2").

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

## Why this ADR does not implement the fix

Per `AGENTS.md` and this session's standing constraints: changes to the live
production mutation authority scope (what a systemd unit an agent daemon is
allowed to restart) are explicitly the kind of change that must not be made
unilaterally — "never self-elevate production autonomy," "never bypass
allowlist," and the general rule that widening or narrowing what a hardened
executor is authorized to touch needs human review, not just green tests. The
current session instead: (a) confirmed and precisely evidenced the gap with
file:line references, (b) proposes the safer of the two structural options, and
(c) leaves the actual `RecoveryGate` change and `runtime_config.py` wiring for
explicit approval, since it changes what the already-deployed VM lab daemons
are authorized to do at the next redeploy.

## Consequences if left unresolved

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
No code was changed. No test was run against this claim beyond re-reading
`tests/test_capability_systemd_restart.py` and `tests/test_aoip_operations.py`
to confirm neither test suite exercises the *other* stack's executor (i.e.
`test_aoip_operations.py`'s `build_recovery_executor` tests never assert an
allowlist check — confirming the gap isn't just untested, it's structurally
absent from that path).
