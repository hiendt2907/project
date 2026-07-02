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
`main` == `origin/main` == `50d2149` (đã push, đã merge `feature/living-operations-runtime` theo
yêu cầu user). `feature/living-operations-runtime` local vẫn ở `ccbb679` (lag phía sau main, không
ảnh hưởng — có thể fast-forward ở phiên sau nếu cần). Commit mới nhất: `50d2149` — supervisor dùng
`--dangerously-skip-permissions` cho invocation `claude -p` (xem "Quyết định đã chốt" bên dưới, đây
là NGOẠI LỆ tường minh do user yêu cầu 2 lần sau khi được cảnh báo rủi ro, không phải mặc định).

## Working tree
Sạch, ngoại trừ 10 file `docs/post-mortems/*.md` modified **có từ trước phiên này** (pre-existing,
timestamp tự cập nhật bởi hook, không liên quan) và thư mục MỚI `.autonomous-loop/` (log runtime
của supervisor đang chạy — KHÔNG commit, đây là log nền của tiến trình, không phải source code).

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
**QUAN TRỌNG — sự cố đã xảy ra và đã fix trong phiên này**: supervisor ban đầu (khởi động lúc
07:25Z, pid 10320) bị KẸT VÒNG LẶP VÔ HẠN không tiến triển — mỗi invocation `claude -p
"/omni-autonomous-productizer one-iteration"` chọn đúng bottleneck (wire `idempotent=True` vào
`src/gateway/routes/autonomy.py:292`) nhưng bị từ chối quyền Edit (vì `-p` không có TTY để duyệt
permission), báo cáo, thoát về IDLE, rồi supervisor gọi lại ngay — lặp lại ~4 lần trong 3 phút,
KHÔNG có thay đổi code nào. Đã dừng supervisor cũ, hỏi user cách xử lý, user chọn tường minh (2 lần,
sau khi được cảnh báo rủi ro) dùng `--dangerously-skip-permissions`. Đã sửa `supervisor.sh` thêm
flag này CHỈ cho invocation `claude -p` của nó (commit `50d2149`), khởi động lại supervisor (pid
19497, đang chạy lúc viết handoff này). Đang chờ xem iteration mới có tiến triển thật không.

**Cập nhật 07:33Z**: bypass-permissions hoạt động — iteration hiện tại (pid biến động, invoke qua
supervisor) lần đầu tiên THẬT SỰ sửa được code, đang viết `scripts/provision_fresh_tenant.py` (mới,
CHƯA COMMIT, còn dang dở khi handoff này được ghi — không phải bug, chỉ là background process chưa
xong). Đây là Phase 4 của slice "Repeatable Tenant Onboarding Baseline" (fresh tenant provisioning
script dùng `idempotent=True` + `scripts/lib/remote_agent_provisioning.py`).

**Session tiếp theo PHẢI**:
1. `bash .claude/skills/omni-autonomous-productizer/scripts/supervisor.sh --status` — còn chạy không.
2. `tail -80 .autonomous-loop/logs/supervisor.log` — xem iteration gần nhất làm được gì, đặc biệt
   có commit nào cho `scripts/provision_fresh_tenant.py` chưa.
3. `git status --short` + `git log --oneline -10` + `docs/operations/AUTONOMOUS_LOOP_STATE.json` —
   supervisor có thể đã tự commit thêm khi bạn không có mặt, hoặc `scripts/provision_fresh_tenant.py`
   vẫn còn untracked/dang dở nếu iteration bị gián đoạn giữa chừng. KHÔNG tin nội dung "Trạng thái
   hiện tại" ở trên nếu git log có commit mới hơn `87e63a2`.
4. Nếu supervisor vẫn chạy với `--dangerously-skip-permissions` không giám sát — đây là quyết định
   user đã chấp nhận rủi ro, KHÔNG tự ý tắt bypass trừ khi user yêu cầu lại, nhưng PHẢI kiểm tra kỹ
   working tree/git log xem nó có làm gì ngoài ý muốn không trước khi tin tưởng bất kỳ commit nào
   nó tạo ra — đặc biệt review nội dung `scripts/provision_fresh_tenant.py` trước khi tin nó an toàn
   (script này có thể gọi API tạo tenant thật — xác nhận nó chỉ target lab, không target production).

Nếu supervisor CHƯA kịp chạy gì mới (kiểm tra bằng git log không có commit mới sau `4185a38`):
tiếp tục slice "Repeatable Tenant Onboarding Baseline" từ Phase 4 (idempotency đã DONE ở
iteration 6, không còn là blocker):
1. Wire `create_tenant(..., idempotent=True)` vào một provisioning caller thật (chưa có caller nào
   dùng flag này — hiện chỉ có test).
2. Phase 4: tạo tenant lab mới thật (`tenant-replay-01`) qua path idempotent, provision agent bằng
   `scripts/lib/remote_agent_provisioning.py` (iteration 5), verify Twin/Competency có dữ liệu thật
   + không cross-tenant contamination với `staging-sim`.
3. Phase 5: chạy provisioning canonical lần 2, verify không duplicate/mất fact.
4. Phase 6: operator read-only proof flow (API/CLI đủ, chưa cần UI).
5. Phase 7: test+deploy+observe (rebuild image, redeploy, quan sát ≥2 discovery cycle).

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
