# Current Session Handoff

## Deliverable phiên này — 3 mảng, đều đã HOÀN TẤT + commit

### 1. Whole-System Reality Audit + Drift Correction Slice
Audit phát hiện runtime lệch tài liệu (CLAUDE.md nói "pod duy nhất" nhưng thực tế nhiều pod;
`OMNI_AUTO_EXECUTE_ENABLED=true` bị bỏ quên từ lab cũ; 5 Deployment zombie; onboarding crash-loop
vì tenant chưa provision). Đã sửa P0 runtime trước, verify, rồi đồng bộ docs.
Chi tiết: `docs/post-mortems/drift-correction-2026-07-02.md` + memory
`project_drift_correction_2026_07_02`. Commit: `0b8fe7c`, `67423b9`.

### 2. Continuous Productization Loop — Iteration 1-4 (golden journey Tenant→Agent→Discovery→Fact→Twin→Competency)
| Iter | Bottleneck | Root cause | Fix | Commit |
|---|---|---|---|---|
| 1 | System Twin trống 100% | Deployment drift — image chưa rebuild sau `1bc6292` | `make docker-worker` + redeploy | `873435b` |
| 2 | `cust-app` không có discovery data | Thiếu `OMNI_REMOTE_DISCOVERY_ENABLED=true` trong `run.env` VM | Sửa VM + restart agent | `8ac9208` |
| 3 | `/onboarding/competency`+`/unknowns` trả 500 | `Dockerfile.gateway` thiếu `COPY src/aoip/` | +1 dòng Dockerfile + rebuild/deploy gateway | `409dcb2` |
| 4 | Mọi Fact có provenance `agent:unknown` | `_project_into_system_model` đọc `agent_id` sai vị trí (nested trong `extracted_fact`, không phải top-level) | Fix `onboarding_pipeline.py` + `schema.py` (promote agent_id trước khi truncate) | *(xem dưới — cần commit)* |

Kết quả hiện tại: Twin đủ 3/3 host thật (76 fact), 0 fact còn `agent:unknown`, operator visibility
qua API thật (`Bearer $OMNI_GATEWAY_API_KEY`) hoạt động. Full pytest suite: 5924 passed, 0 fail.
Chi tiết đầy đủ: `docs/product/PRODUCT_PROOF.md` + memory `project_productization_iteration{1,2,3,4}_*`.

**Phát hiện thêm chưa fix (iteration 5 candidate):** `coerce_evidence_dict()` (`pkg/reasoning/schema.py`)
cắt cứng `extracted_fact` ở 2000 ký tự — nếu `discovery_data` lớn (process_list dài), JSON bị cắt
giữa chừng → `json.loads()` lỗi → mất TOÀN BỘ evidence (không chỉ agent_id). Chưa fire trong lab
hiện tại (payload nhỏ) nhưng là rủi ro thật. Xem `project_productization_iteration4_provenance_fix`.

### 3. `omni-lane-operator-loop` health-check (iter27)
Chạy 3 lane diagnostic (sys_resource/sys_hard_fail/siem_security) qua Admin Simulator, xác nhận
không regression từ các thay đổi runtime ở mục 1-2 (kill-switch revert, xóa zombie deployment,
rebuild gateway/onboarding/fullstack). 12/12 stage resolved cả 3 lane, CRAT signed, Telegram gửi
thật. Không có finding mới. Ledger: `project_lane_operator_loop_ledger.md` (session 2026-07-02 iter27).

## Trạng thái hiện tại
- Đã commit: `0b8fe7c`, `67423b9` (drift correction), `873435b` (iter1), `8ac9208` (iter2),
  `409dcb2` (iter3).
- **CHƯA COMMIT:** fix iteration 4 (`src/workers/onboarding_pipeline.py`,
  `src/pkg/reasoning/schema.py`, `tests/test_onboarding_pipeline.py`) + cập nhật
  `docs/product/PRODUCT_PROOF.md` + memory. Full suite đã xanh (5924 passed) — sẵn sàng commit
  ngay đầu session sau nếu chưa kịp làm trong phiên này.
- Runtime: `omni-fullstack`/`omni-onboarding` chạy digest `943043a3ef3b...` (bao gồm cả fix iter4),
  `omni-gateway` chạy digest `24a0047be646...` (bao gồm fix iter3 + pipeline_stages từ iter26).
  `OMNI_AUTO_EXECUTE_ENABLED=false`, tier hiệu lực = Redis cache `shadow`.

## Quyết định đã chốt (toàn phiên)
- Runtime truth sửa trước, tài liệu chỉ cập nhật sau khi verify xong.
- Không tạo bảng/schema ad hoc — dùng đúng cơ chế canonical đã có trong repo
  (`AdminConfigRepo.create_tenant()`, `run_migrations()`).
- Không xóa Deployment chỉ vì `replicas=0` — bắt buộc đối chiếu git history + PDB/Service trước.
- Không đổi Kafka partition/replication (P1 riêng, không gây mất dữ liệu hiện tại).
- Không làm O2B/O2C cho tới khi golden journey Tenant→Agent→Discovery→Fact→Twin→Competency
  "sạch" (operator-visible, không còn provenance/UX gap lớn).
- Mỗi lần runtime thiếu dữ liệu bất thường: LUÔN nghi ngờ deployment drift trước
  (`hasattr()`/`inspect.getsource()` trong pod đang chạy) trước khi sửa logic code — đã đúng 2/4
  iteration.

## Blockers
Không có.

## Next step chính xác — Iteration 5 của Continuous Productization Loop
1. **Commit fix iteration 4** nếu chưa làm (xem "Trạng thái hiện tại").
2. **Bottleneck đề xuất (chọn 1, không mở song song):**
   - Fix rủi ro truncation trong `coerce_evidence_dict()` (mục "Phát hiện thêm chưa fix" ở trên) —
     ưu tiên cao vì có thể mất evidence hoàn toàn cho VM có nhiều process/service hơn 3 VM lab hiện tại.
   - UX gap `/onboarding/competency`: `entity_id` yêu cầu format `host:cust-app` thay vì `cust-app`
     — dễ gây operator hiểu nhầm "không có data".
   - Provisioning gap `OMNI_REMOTE_DISCOVERY_ENABLED`: `scripts/e2e_orbstack_fleet.py` không set biến
     này — VM thứ 4 sẽ lặp lại gap iteration 2.
3. Chỉ sau khi các gap này dọn xong mới quay lại quyết định O2B (source acquisition planner).

## Không được làm lại
- Không audit lại pipeline discovery→onboarding→SystemModel→CompetencyMatrix→Question từ đầu (đã
  map đủ qua O1/O2A/O2B + 4 iteration productization phiên này).
- Không coi VM lab là BLOCKED — access method đúng là `orb -m <machine> <command>`.
- Không tự tạo lại Deployment `omni-analyst/core/executor/prober/worker` — đã xác nhận RETIRED qua
  git history (`915e509`).
- Không revert `OMNI_AUTO_EXECUTE_ENABLED` về `true` mà không có yêu cầu tường minh mới từ user.
- Không giả định code local chưa deploy đúng chỉ vì runtime thiếu dữ liệu — luôn verify trong pod
  trước khi sửa logic.
- Không tạo bảng/schema Postgres bằng SQL thủ công.
- Không mở rộng fix "coerce_evidence_dict truncation" thành sửa cả `discovery_data` truncation
  trong cùng 1 iteration nếu chọn nó — đây là 2 vấn đề tách biệt được ghi nhận riêng.

## Tài liệu liên quan
- `docs/post-mortems/drift-correction-2026-07-02.md`, `docs/product/PRODUCT_PROOF.md`.
- Memory: `project_drift_correction_2026_07_02`,
  `project_productization_iteration{1_twin,2_custapp,3_gateway_aoip_import,4_provenance_fix}`,
  `project_lane_operator_loop_ledger` (iter27).
- `CLAUDE.md` mục "DEPLOYMENT STATE" (2026-07-02) — topology declared/deployed/retired hiện tại.
