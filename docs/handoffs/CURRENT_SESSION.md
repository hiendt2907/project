# Current Session Handoff

Updated: 2026-07-15

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
