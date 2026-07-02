# Current Session Handoff

## Update iteration 9 (2026-07-02T18:05Z)
Phase 6 của slice "Repeatable Tenant Onboarding Baseline" DONE ở mức scoped: cài agent thứ hai
(`omni-remote-agent-replay01.service`) trên VM `cust-edge` bind vào `tenant-replay-01`, chạy song
song agent staging-sim có sẵn. Runtime proof đầy đủ: register/evidence 200 OK, Twin
(`omni:aoip:system_model:tenant-replay-01`) có 41 fact/revision=6/chỉ host cust-edge, cách ly xác
nhận với `staging-sim` (Twin 78 fact/3 host không đổi). API `/onboarding/unknowns`,
`/onboarding/competency` verify sống cho tenant mới. Chi tiết đầy đủ + 1 UX-gap phát hiện
(`resolve_scope()` silent override, không phải bug) → `docs/product/PRODUCT_PROOF.md` mục
"Iteration 9". Full suite: 5948 passed (1 flake cũ, không đổi). CHƯA làm: multi-host cho
tenant-replay-01 (hiện chỉ 1/1 host), script hoá bước thêm API key vào `omni-gateway-secret`.
Next step chi tiết ở `docs/operations/AUTONOMOUS_LOOP_STATE.json` → `iteration.next_step`.

## Deliverable hiện tại
Slice "Repeatable Tenant Onboarding Baseline" (iteration 5-6 của Continuous Productization Loop) +
skill mới `.claude/skills/omni-autonomous-productizer/` (autonomous productization loop operator).
Mục tiêu slice: chứng minh tenant lab mới đi hết Tenant→Agent→Discovery→Fact→Twin→Competency mà
không sửa tay. Iteration 1-4 trước đó đã DONE (System Twin persistence, cust-app discovery,
gateway/aoip import, Fact provenance).

## Definition of Done
Mỗi iteration: bottleneck → root cause → fix → runtime proof (không chỉ test/deploy) →
docs/memory cập nhật → commit riêng. Đã đạt cho iteration 1-4. Iteration 5 (slice 7-phase) đạt
Phase 1-3/7. Iteration 6 (idempotent tenant creation) DONE ở mức `VERIFIED_TEST` — xem "Trạng thái
hiện tại".

## Trạng thái hiện tại
Iteration 5 — Phase 1 (Inspect) + Phase 2 (safe evidence compaction) + Phase 3 (canonical
provisioning module) DONE + test xanh. Phase 4-7 (fresh tenant thật trên VM/cluster, repeatability,
operator proof, deploy+observe) CHƯA làm — cần VM/cluster thật.
Iteration 6 — bootstrap skill `omni-autonomous-productizer` (validated + smoke-tested read-only
live trên cluster/OrbStack thật) rồi dùng chính skill đó chạy 1 iteration thật:
`AdminConfigRepo.create_tenant(idempotent=True)` — mở khóa Phase 5 (repeat provisioning) nhưng
CHƯA wire vào caller thật nào, CHƯA runtime-verify trên Postgres thật (chỉ FakePgPool trong test).
KHÔNG được coi golden journey là "repeatable" cho tới khi Phase 4-7 của slice chạy xong. Chi tiết:
`docs/product/PRODUCT_PROOF.md` mục "Iteration 5" và "Iteration 6".

## Đã hoàn thành
- Drift Correction: kill-switch `OMNI_AUTO_EXECUTE_ENABLED` revert true→false; tenant `staging-sim`
  provisioned; 5 zombie Deployment xóa; docs CLAUDE.md đồng bộ topology thật.
- Iter1: rebuild+redeploy `omni-onboarding`/`omni-fullstack` → System Twin có dữ liệu thật (deployment
  drift, không phải code bug).
- Iter2: fix `OMNI_REMOTE_DISCOVERY_ENABLED` thiếu trên VM `cust-app` → Twin đủ 3/3 host.
- Iter3: `Dockerfile.gateway` thiếu `COPY src/aoip/` → fix 1 dòng → Competency/Unknowns API sống.
- Iter4: `agent:unknown` provenance → fix `onboarding_pipeline.py` + `schema.py` → 0/76 fact còn
  `agent:unknown`. Full suite 5924 passed.
- `omni-lane-operator-loop` iter27: health-check 3 lane diagnostic, PASS, không regression.
- Báo cáo tổng kết toàn phiên đã gửi user (report-only, evidence-based, verdict VERIFIED_RUNTIME/
  PARTIAL/CODE_ONLY/ABSENT theo từng capability).

## Branch và commit
`main` HEAD hiện tại = `7689049` ("Add provider lab incident endpoints"). Lịch sử gần nhất:
`7689049` ← `8d4c0ed` (feat(portal): runtime-backed understanding + human claim workflow with
codex) ← `c7c66eb` (docs: reconcile stale state.json/ledger) ← `8e15178` (iter7 fresh-tenant
provisioning) ← `0d7d352` ← `c7c66eb`. **Lưu ý**: `8d4c0ed` và `7689049` là công việc
provider-portal / lab-incidents (`src/aoip/console/`, `ui/apps/provider-portal/`) làm NGOÀI vòng
lặp `omni-autonomous-productizer` (không đụng tới onboarding/Twin/Competency/tenant-replay-01) —
được phát hiện qua drift reconciliation ở iter8, không phải bug, chỉ là công việc song song đã
merge vào `main`. Chưa xác nhận đã push lên `origin/main` hay chưa — kiểm tra `git fetch && git
status` ở phiên sau trước khi giả định đồng bộ.

## Working tree
Sạch, ngoại trừ 10 file `docs/post-mortems/*.md` modified **có từ trước phiên này** (pre-existing,
timestamp tự cập nhật bởi hook, không liên quan) và thư mục `.autonomous-loop/` (log runtime của
supervisor — KHÔNG commit, log nền của tiến trình, không phải source code).

## Files chính đã thay đổi (iteration 5-8, đã commit local)
Iteration 5 (`e8a8c96`): `src/pkg/reasoning/schema.py` (`_compact_extracted_fact()`/
`_compact_value()` thay slicing thô, thêm `schema_version`/`truncated`/`original_size`/
`content_hash`); `scripts/lib/remote_agent_provisioning.py` (MỚI —
`AgentProvisioningSpec`/`render_run_env()`/`is_idempotent_rewrite()`/`effective_config_summary()`);
`scripts/e2e_onboarding_full_flow.py` (TC-OB02 dùng module mới); `tests/test_evidence_compaction.py`,
`tests/test_remote_agent_provisioning.py` (MỚI).

Iteration 6 — skill bootstrap (`5c76425`): `.claude/skills/omni-autonomous-productizer/` (MỚI, 18
file: SKILL.md + 6 references/ + 4 templates/ + 5 scripts/); `docs/operations/AUTONOMOUS_LOOP_STATE.json`,
`docs/operations/AUTONOMOUS_LOOP_LEDGER.md` (MỚI).

Iteration 6 — tenant idempotency (`61fcdcb`, `f806af0`): `src/services/admin_config/repo.py`
(`create_tenant(..., idempotent: bool = False)` opt-in, giữ nguyên HTTP 409 contract khi
`idempotent=False`); `tests/test_admin_config_store.py` (test mới
`test_create_tenant_idempotent_true_is_repeatable`); `docs/product/PRODUCT_PROOF.md` (mục
"Iteration 5" + "Iteration 6"); `.claude/skills/omni-autonomous-productizer/references/current-priority.md`;
`docs/operations/AUTONOMOUS_LOOP_STATE.json` (status=IDLE).

Iteration 7 (`8e15178`, đã commit trước phiên này): `scripts/provision_fresh_tenant.py` (MỚI —
canonical caller gọi `AdminConfigRepo.create_tenant(idempotent=True)` qua asyncpg pool thật);
`VERIFIED_RUNTIME` trên Postgres thật cho tenant `tenant-replay-01` (1 row tenant, 1 audit event
sau 2 lần gọi). Xem `docs/product/PRODUCT_PROOF.md` mục "Iteration 7".

Iteration 8 (`7689049`, reconciliation-only — không phải công việc của skill này, phát hiện qua
drift check): `8d4c0ed` + `7689049` thêm provider-portal understanding/human-claim workflow +
lab-incident endpoints (`src/aoip/console/{agents,understanding,human_inbox,lab_incidents}.py`,
`ui/apps/provider-portal/**`). Skill `omni-autonomous-productizer` chỉ cập nhật
`docs/operations/AUTONOMOUS_LOOP_STATE.json` + `AUTONOMOUS_LOOP_LEDGER.md` để phản ánh HEAD thật —
không viết code mới. Full suite re-verify: 5948 passed, 5 deselected, 1 failed (flake đã biết).

## Quyết định đã chốt
- Không làm O2B/O2C (source acquisition planner) cho tới khi golden journey hiện tại "sạch"
  (operator-visible, không còn provenance/UX gap lớn).
- Không mở rộng fix "coerce_evidence_dict truncation" (candidate iter5) thành sửa luôn
  `discovery_data` truncation trong cùng 1 iteration — 2 vấn đề tách biệt.
- Không tạo bảng/schema Postgres ad hoc, không xóa Deployment chỉ vì `replicas=0` — luôn đối chiếu
  git history/PDB trước.
- Mỗi lần runtime thiếu dữ liệu bất thường: nghi ngờ deployment drift trước
  (`hasattr()`/`inspect.getsource()` trong pod) trước khi sửa logic — đúng 2/4 iteration phiên này.
- **`supervisor.sh` dùng `--dangerously-skip-permissions`** cho invocation `claude -p` của nó
  (2026-07-02) — user yêu cầu tường minh 2 lần sau khi được cảnh báo rủi ro (đã đề xuất phương án an
  toàn hơn là scoped `--allowedTools` allowlist, user từ chối, chọn full bypass). Đây là ngoại lệ
  CHỈ áp dụng cho supervisor's non-interactive invocations, không áp dụng cho phiên tương tác
  thường, không liên quan tới `OMNI_AUTO_EXECUTE_ENABLED` (kill-switch K8s riêng biệt). Không tự ý
  gỡ bỏ flag này ở phiên sau trừ khi user yêu cầu — nhưng LUÔN kiểm tra kỹ log/git trước khi tin bất
  kỳ thay đổi nào supervisor tự tạo ra khi không giám sát.

## Verification đã chạy
`.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` (sau iteration 6) →
**5940 passed, 6 deselected, 0 failed** (1 test flaky trước-đã-tồn-tại
`test_remote_agent_e2e.py::...::test_register_then_real_system_metrics_emitted_through_real_pipeline`
— confirmed KHÔNG liên quan bằng `git stash` + chạy lại trên commit trước khi sửa, cùng lỗi tái
hiện, đã deselect trong lần chạy full). 16 test iteration 5 (9 compaction + 7 provisioning) + 1 test
iteration 6 (`test_create_tenant_idempotent_true_is_repeatable`) đều pass. Skill
`omni-autonomous-productizer` smoke-tested: `validate_state.py --print` OK, `calculate_sleep.py
--print-only` OK, `reality_check.sh` chạy LIVE read-only trên cluster thật (3 VM OrbStack Running,
`OMNI_AUTO_EXECUTE_ENABLED=false` xác nhận trên env thật của `omni-fullstack`, không phải chỉ đọc
manifest). CHƯA có runtime proof trên VM/cluster thật cho iteration 5 Phase 4-7, và
`create_tenant(idempotent=True)` của iteration 6 CHƯA runtime-verify trên Postgres thật (chỉ
FakePgPool trong test) và CHƯA được wire vào bất kỳ caller thật nào.

## Deployment hiện tại
`omni-fullstack`/`omni-onboarding` chạy digest `943043a3ef3b...` (bao gồm fix iter4).
`omni-gateway` chạy digest `24a0047be646...` (bao gồm fix iter3). `OMNI_AUTO_EXECUTE_ENABLED=false`,
tier hiệu lực = Redis cache `shadow`. Namespace `multi-agent`, cluster OrbStack lab.

## Blockers
Không có.

## Next step chính xác
Supervisor incident (báo cáo trước đây trong handoff này) đã kết thúc và đã bị vượt qua bởi các
iteration sau — iter7 commit thành công (`8e15178`), và 2 commit ngoài-loop `8d4c0ed`/`7689049`
(provider-portal work) đã landed on `main` sau đó mà không liên quan tới supervisor. HEAD hiện tại
đã xác nhận = `7689049`. `.autonomous-loop/supervisor.lock` tồn tại trong `7689049` — session sau
PHẢI chạy `bash .claude/skills/omni-autonomous-productizer/scripts/supervisor.sh --status` trước để
biết loop nền còn chạy không trước khi tự invoke `one-iteration` thủ công (tránh đụng độ 2 tiến
trình sửa cùng lúc).

**Bottleneck kế tiếp (không đổi từ iter7, chưa ai làm)** — Phase 6 của slice "Repeatable Tenant
Onboarding Baseline": `tenant-replay-01` hiện chỉ có 1 row `omni_admin.tenant` (từ iter7), CHƯA có
Agent/VM/discovery/Twin/Competency thật gắn vào. Việc cần làm:
1. Quyết định reuse VM lab hiện có (`cust-edge`/`cust-app`/`cust-db`) với agent identity thứ 2 hay
   provision VM mới trong OrbStack — inspect `scripts/e2e_orbstack_fleet.py` trước khi chọn.
2. Provision Agent cho `tenant-replay-01` qua `scripts/lib/remote_agent_provisioning.py` (iteration
   5, đã có `discovery_enabled: bool = True` mặc định — không lặp lại gap cust-app cũ).
3. Chạy golden journey Tenant→Agent→Discovery→Fact→Twin→Competency không sửa tay, quan sát runtime
   thật (không chỉ log tồn tại).
4. Chứng minh cross-tenant isolation: Twin/Competency của `tenant-replay-01` và `staging-sim` không
   lẫn dữ liệu (keyed đúng `tenant_id`).
5. Chứng minh operator read-only proof flow: `GET /onboarding/competency` + `/unknowns` scoped
   đúng `tenant-replay-01`.

Đây là slice nhiều bước (VM/agent provisioning + quan sát ≥1 discovery cycle thật) — không cố nhồi
vào 1 `one-iteration` ngắn, chia nhỏ nếu cần nhưng mỗi checkpoint phải để lại trạng thái sạch (không
dở dang giữa VM provisioning và Agent enrollment).

## Lệnh cần chạy lại
`bash .claude/skills/omni-autonomous-productizer/scripts/supervisor.sh --status` để biết loop nền
còn chạy không. `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` để xác nhận
baseline vẫn xanh trước khi tiếp tục thủ công bất kỳ việc gì (tránh đụng độ với supervisor đang
chạy song song).

## Không được làm lại
- Không audit lại pipeline discovery→onboarding→SystemModel→CompetencyMatrix→Question từ đầu.
- Không coi VM lab là BLOCKED — access method đúng là `orb -m <machine> <command>`.
- Không tự tạo lại Deployment `omni-analyst/core/executor/prober/worker` (đã RETIRED, xác nhận qua
  git history `915e509`).
- Không giả định code local chưa deploy đúng chỉ vì runtime thiếu dữ liệu — luôn verify trong pod.
- Không revert `OMNI_AUTO_EXECUTE_ENABLED` về `true` mà không có yêu cầu tường minh mới từ user.

## Tài liệu liên quan
- `docs/product/PRODUCT_PROOF.md` — capability matrix + golden journey status, mục "Iteration 5" và
  "Iteration 6".
- `docs/post-mortems/drift-correction-2026-07-02.md`.
- `.claude/skills/omni-autonomous-productizer/` — skill mới (iteration 6), đọc `SKILL.md` +
  `references/current-priority.md` trước khi chọn bottleneck tiếp theo.
- `docs/operations/AUTONOMOUS_LOOP_STATE.json`, `docs/operations/AUTONOMOUS_LOOP_LEDGER.md` — state
  machine + ledger của skill trên, source of truth cho next_step.
- Memory: `project_drift_correction_2026_07_02`,
  `project_productization_iteration{1_twin,2_custapp,3_gateway_aoip_import,4_provenance_fix}`,
  `project_lane_operator_loop_ledger` (session iter27).
- `CLAUDE.md` mục "DEPLOYMENT STATE" (2026-07-02).
