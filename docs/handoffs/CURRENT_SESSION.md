# Current Session Handoff

Updated: 2026-07-14

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

## Known follow-ups

- Clean the non-blocking deprecation and unawaited-`AsyncMock` warnings.
- Reconcile the accumulated open onboarding questions and verify stale replay-agent
  heartbeat records.
- Keep autonomy in shadow/kill-switch mode until an explicit production-governance
  decision is made.
