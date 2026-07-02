# Current Session Handoff

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
`feature/living-operations-runtime` @ `f806af0` (local, CHƯA push — 4 commit sau `07f2eed`:
`e8a8c96` iter5 Phase1-3, `5c76425` skill bootstrap, `61fcdcb` iter6 idempotent tenant, `f806af0`
loop-state IDLE). Push cần chỉ thị rõ từ user.

## Working tree
Sạch, ngoại trừ 10 file `docs/post-mortems/*.md` modified **có từ trước phiên này** (pre-existing,
không liên quan, không chạm trong phiên).

## Files chính đã thay đổi (iteration 5-6, đã commit local)
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

## Quyết định đã chốt
- Không làm O2B/O2C (source acquisition planner) cho tới khi golden journey hiện tại "sạch"
  (operator-visible, không còn provenance/UX gap lớn).
- Không mở rộng fix "coerce_evidence_dict truncation" (candidate iter5) thành sửa luôn
  `discovery_data` truncation trong cùng 1 iteration — 2 vấn đề tách biệt.
- Không tạo bảng/schema Postgres ad hoc, không xóa Deployment chỉ vì `replicas=0` — luôn đối chiếu
  git history/PDB trước.
- Mỗi lần runtime thiếu dữ liệu bất thường: nghi ngờ deployment drift trước
  (`hasattr()`/`inspect.getsource()` trong pod) trước khi sửa logic — đúng 2/4 iteration phiên này.

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
Có 2 lựa chọn khởi động cho session tiếp theo (cả hai đều hợp lệ — skill mới có thể tự chọn qua
Reality Map, hoặc tiếp tục thủ công theo slice cũ):

**A. Dùng skill mới**: `/omni-autonomous-productizer resume` hoặc `start` — sẽ tự đọc
`docs/operations/AUTONOMOUS_LOOP_STATE.json` (status=IDLE, next_step đã ghi rõ), dựng lại Reality
Map, và tiếp tục.

**B. Tiếp tục thủ công slice "Repeatable Tenant Onboarding Baseline" từ Phase 4** (idempotency đã
DONE ở iteration 6, không còn là blocker):
1. Wire `create_tenant(..., idempotent=True)` vào một provisioning caller thật (chưa có caller nào
   dùng flag này — hiện chỉ có test).
2. Phase 4: tạo tenant lab mới thật (`tenant-replay-01`) qua path idempotent, provision agent bằng
   `scripts/lib/remote_agent_provisioning.py` (iteration 5), verify Twin/Competency có dữ liệu thật
   + không cross-tenant contamination với `staging-sim`.
3. Phase 5: chạy provisioning canonical lần 2, verify không duplicate/mất fact (giờ có
   `idempotent=True` để dùng).
4. Phase 6: operator read-only proof flow (API/CLI đủ, chưa cần UI).
5. Phase 7: test+deploy+observe (rebuild image, redeploy, quan sát ≥2 discovery cycle).

## Lệnh cần chạy lại
`.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` để xác nhận baseline vẫn xanh
trước khi tiếp tục Phase 4. Cân nhắc `git push origin feature/living-operations-runtime` (4 commit
local chưa push: `e8a8c96`, `5c76425`, `61fcdcb`, `f806af0`) — cần user xác nhận trước.

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
