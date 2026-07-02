# Current Session Handoff

## Deliverable hiện tại
Slice "Repeatable Tenant Onboarding Baseline" (iteration 5 của Continuous Productization Loop).
Mục tiêu: chứng minh tenant lab mới đi hết Tenant→Agent→Discovery→Fact→Twin→Competency mà không
sửa tay. Iteration 1-4 trước đó đã DONE (System Twin persistence, cust-app discovery, gateway/aoip
import, Fact provenance).

## Definition of Done
Mỗi iteration: bottleneck → root cause → fix → runtime proof (không chỉ test/deploy) →
docs/memory cập nhật → commit riêng. Đã đạt cho iteration 1-4. Iteration 5 (slice 7-phase) MỚI đạt
Phase 1-3/7 — xem "Trạng thái hiện tại".

## Trạng thái hiện tại
Iteration 5 — Phase 1 (Inspect) + Phase 2 (safe evidence compaction) + Phase 3 (canonical
provisioning module) DONE + test xanh. Phase 4-7 (fresh tenant thật trên VM/cluster, repeatability,
operator proof, deploy+observe) CHƯA làm — cần VM/cluster thật, không đủ trong lượt này. KHÔNG được
coi golden journey là "repeatable" cho tới khi Phase 4-7 chạy xong. Chi tiết: `docs/product/PRODUCT_PROOF.md` mục "Iteration 5".

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
`feature/living-operations-runtime` @ `07f2eed` (đã push). Thay đổi iteration 5 (schema.py,
provisioning module, tests, docs) CHƯA commit — xem "Files chính đã thay đổi".

## Working tree
Có thay đổi chưa commit: `src/pkg/reasoning/schema.py` (safe compaction), `scripts/lib/remote_agent_provisioning.py` (mới), `scripts/e2e_onboarding_full_flow.py` (dùng module mới),
`tests/test_evidence_compaction.py` (mới), `tests/test_remote_agent_provisioning.py` (mới),
`docs/product/PRODUCT_PROOF.md`.

## Files chính đã thay đổi (iteration 5, chưa commit)
`src/pkg/reasoning/schema.py` — `_compact_extracted_fact()`/`_compact_value()` thay slicing thô,
thêm `schema_version`/`truncated`/`original_size`/`content_hash`.
`scripts/lib/remote_agent_provisioning.py` — MỚI, `AgentProvisioningSpec`/`render_run_env()`/
`is_idempotent_rewrite()`/`effective_config_summary()`.
`scripts/e2e_onboarding_full_flow.py` — TC-OB02 dùng module thay vì f-string tay.
`tests/test_evidence_compaction.py`, `tests/test_remote_agent_provisioning.py` — MỚI.
`docs/product/PRODUCT_PROOF.md` — mục "Iteration 5" + known-broken-links cập nhật.

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
`.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` (sau iteration 5 Phase 1-3) →
**5939 passed, 6 deselected, 0 failed** (1 test flaky trước-đã-tồn-tại
`test_remote_agent_e2e.py::...::test_register_then_real_system_metrics_emitted_through_real_pipeline`
— confirmed KHÔNG liên quan (routing METRIC_SAMPLE vs ANOMALY phụ thuộc tải máy thật, reproduce cả
trên commit trước khi sửa), đã deselect trong lần chạy full). 16 test mới cho iteration 5 (9
compaction + 7 provisioning) đều pass riêng lẻ. CHƯA có runtime proof trên VM/cluster thật cho
iteration 5 (Phase 4-7 chưa chạy).

## Deployment hiện tại
`omni-fullstack`/`omni-onboarding` chạy digest `943043a3ef3b...` (bao gồm fix iter4).
`omni-gateway` chạy digest `24a0047be646...` (bao gồm fix iter3). `OMNI_AUTO_EXECUTE_ENABLED=false`,
tier hiệu lực = Redis cache `shadow`. Namespace `multi-agent`, cluster OrbStack lab.

## Blockers
Không có.

## Next step chính xác
Tiếp tục slice "Repeatable Tenant Onboarding Baseline" từ Phase 4:
1. **Trước Phase 4**: fix idempotency `AdminConfigRepo.create_tenant()`
   (`src/services/admin_config/repo.py:574-578`, hiện raise `ValueError` nếu tenant tồn tại) — chặn
   Phase 5 (repeat provisioning) nếu không sửa trước.
2. Phase 4: tạo tenant lab mới thật (`tenant-replay-01`) qua `POST /autonomy/tenants`, provision
   agent bằng `scripts/lib/remote_agent_provisioning.py` (mới, iteration 5), verify Twin/Competency
   có dữ liệu thật + không cross-tenant contamination với `staging-sim`.
3. Phase 5: chạy provisioning canonical lần 2, verify không duplicate/mất fact.
4. Phase 6: operator read-only proof flow (API/CLI đủ, chưa cần UI).
5. Phase 7: test+deploy+observe (rebuild image, redeploy, quan sát ≥2 discovery cycle).
Commit iteration 5 Phase 1-3 (schema.py, provisioning module, tests, docs) TRƯỚC khi bắt đầu Phase 4
nếu chưa commit.

## Lệnh cần chạy lại
`.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` để xác nhận baseline vẫn xanh
trước khi tiếp tục Phase 4.

## Không được làm lại
- Không audit lại pipeline discovery→onboarding→SystemModel→CompetencyMatrix→Question từ đầu.
- Không coi VM lab là BLOCKED — access method đúng là `orb -m <machine> <command>`.
- Không tự tạo lại Deployment `omni-analyst/core/executor/prober/worker` (đã RETIRED, xác nhận qua
  git history `915e509`).
- Không giả định code local chưa deploy đúng chỉ vì runtime thiếu dữ liệu — luôn verify trong pod.
- Không revert `OMNI_AUTO_EXECUTE_ENABLED` về `true` mà không có yêu cầu tường minh mới từ user.

## Tài liệu liên quan
- `docs/product/PRODUCT_PROOF.md` — capability matrix + golden journey status đầy đủ 4 iteration.
- `docs/post-mortems/drift-correction-2026-07-02.md`.
- Memory: `project_drift_correction_2026_07_02`,
  `project_productization_iteration{1_twin,2_custapp,3_gateway_aoip_import,4_provenance_fix}`,
  `project_lane_operator_loop_ledger` (session iter27).
- `CLAUDE.md` mục "DEPLOYMENT STATE" (2026-07-02).
