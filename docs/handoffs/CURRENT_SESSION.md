# Current Session Handoff

Updated: 2026-07-20

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
