# Current Session Handoff

## Deliverable hiện tại
Continuous Productization Loop cho golden journey `Tenant→Agent→Discovery→Fact→Twin→Competency`.
Iteration 1-4 đã xong (System Twin persistence, cust-app discovery, gateway/aoip import, Fact
provenance). Trước đó trong cùng phiên: Whole-System Reality Audit + Drift Correction Slice +
`omni-lane-operator-loop` health-check (iter27).

## Definition of Done
Mỗi iteration: bottleneck → root cause → fix → runtime proof (không chỉ test/deploy) →
docs/memory cập nhật → commit riêng. Đã đạt cho iteration 1-4.

## Trạng thái hiện tại
4 iteration productization đã DONE + verified + committed. Golden journey chạy thật tới
Competency/Unknown qua API (`GET /onboarding/competency`, `/unknowns`). Chưa có UI operator, chưa
có Handover/Daily-Operations nào được implement hay kiểm chứng. Sẵn sàng bắt đầu iteration 5.

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
`feature/living-operations-runtime` @ `435b377` (không có commit chưa push riêng — chưa `git push`
trong phiên này, xem "Lệnh cần chạy lại" nếu cần đồng bộ remote).

## Working tree
Sạch, ngoại trừ 10 file `docs/post-mortems/*.md` modified **có từ trước phiên này** (không liên
quan, không chạm trong phiên).

## Files chính đã thay đổi
`src/workers/onboarding_pipeline.py`, `src/pkg/reasoning/schema.py`,
`tests/test_onboarding_pipeline.py`, `Dockerfile.gateway`, `CLAUDE.md`,
`docs/product/PRODUCT_PROOF.md`, `docs/post-mortems/drift-correction-2026-07-02.md`,
`k8s/services/omni-analyst-service.yaml` (xóa).

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
`.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` (sau iter4) → **5924 passed, 5
deselected, 0 failed**. Runtime: `redis-cli HGET omni:aoip:system_model:staging-sim facts` → 0/76
fact còn `agent:unknown`. API thật: `curl -H "Authorization: Bearer $KEY"
.../onboarding/competency?tenant_id=staging-sim&entity_type=host&entity_id=host:cust-app` →
`identity: VERIFIED`.

## Deployment hiện tại
`omni-fullstack`/`omni-onboarding` chạy digest `943043a3ef3b...` (bao gồm fix iter4).
`omni-gateway` chạy digest `24a0047be646...` (bao gồm fix iter3). `OMNI_AUTO_EXECUTE_ENABLED=false`,
tier hiệu lực = Redis cache `shadow`. Namespace `multi-agent`, cluster OrbStack lab.

## Blockers
Không có.

## Next step chính xác
Bắt đầu Iteration 5 — chọn 1 trong 3 bottleneck đã liệt kê (không mở song song):
1. Fix rủi ro truncation trong `coerce_evidence_dict()` (`src/pkg/reasoning/schema.py`) — JSON có
   thể invalid nếu `discovery_data` lớn, mất toàn bộ evidence (ưu tiên cao nhất, đã có test chứng
   minh rủi ro nhưng chưa fire trong lab).
2. UX gap `/onboarding/competency`: `entity_id` yêu cầu format `host:cust-app` thay vì `cust-app`.
3. Provisioning gap: `scripts/e2e_orbstack_fleet.py` không set `OMNI_REMOTE_DISCOVERY_ENABLED` — VM
   thứ 4 sẽ lặp lại gap iter2.

## Lệnh cần chạy lại
`.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` để xác nhận baseline vẫn xanh
trước khi bắt đầu iteration 5. Cân nhắc `git push origin feature/living-operations-runtime` nếu
muốn đồng bộ remote (chưa làm trong phiên này, cần user xác nhận).

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
