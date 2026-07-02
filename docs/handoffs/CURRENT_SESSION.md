# Current Session Handoff

## Deliverable hiện tại (phiên này) — 2 phần, cả hai HOÀN TẤT giai đoạn 1

### Phần 1: Runtime Drift Correction and Safety Restoration — HOÀN TẤT
Sau Whole-System Reality Audit phát hiện runtime lệch tài liệu, đã sửa P0 runtime trước, xác minh
lại, rồi đồng bộ tài liệu trong cùng slice. Chi tiết đầy đủ: `docs/post-mortems/drift-correction-2026-07-02.md`
+ memory `project_drift_correction_2026_07_02`.

### Phần 2: Continuous Productization Loop — Iteration 1 + 2 HOÀN TẤT

**Iteration 1:** System Twin (`omni:aoip:system_model:*`) trống hoàn toàn dù O1/O2A/O2B claim DONE.
**Root cause: deployment drift** — image `multi-agent-system:latest` chưa rebuild kể từ `1bc6292`
(pod thiếu hẳn `_project_into_system_model`, xác minh bằng `hasattr()` trực tiếp trong pod). Đã
`make docker-worker` + redeploy `omni-onboarding`+`omni-fullstack` → digest mới `c2d433daac77...`.
Twin có dữ liệu cho 2/3 host (cust-edge, cust-db) — cust-app thiếu.

**Iteration 2:** Điều tra cust-app → root cause: `/opt/omni-remote-agent/run.env` trên cust-app
**thiếu dòng `OMNI_REMOTE_DISCOVERY_ENABLED=true`** (cust-edge/cust-db đều có, cust-app không —
gap lúc provision VM, không phải bug code — `src/remote_agent/settings.py` default `false`). Đã
thêm dòng đó + `systemctl restart omni-remote-agent` trên cust-app. **Runtime proof: Twin nay có
đủ 3/3 host, 76 fact, revision 54, `host:cust-app` → `exposes_port 8080` khớp `ss -lntp` đã chạy
trên VM.**

Chi tiết đầy đủ cả 2 iteration: `docs/product/PRODUCT_PROOF.md` (capability matrix + golden journey
status, mỗi dòng trỏ evidence cụ thể) + memory `project_productization_iteration1_twin` +
`project_productization_iteration2_custapp`.

**Không commit code Python nào trong cả 2 iteration** — iteration 1 là deploy-only (code local đã
đúng), iteration 2 là sửa config trên VM lab (không phải file trong git repo này — `run.env` sống
trên VM, không phải source-controlled). Chỉ commit docs.

## Tóm tắt kết quả (before → after)

| Check | Before | After | Verdict |
|---|---|---|---|
| `OMNI_AUTO_EXECUTE_ENABLED` (pod thật) | `true` (bỏ quên từ lab session 2026-06-11) | `false` | ✅ FIXED, verified trên pod mới |
| `OMNI_SIEM_SUGGEST_ONLY` | `false` | `true` | ✅ FIXED |
| `OMNI_AUTONOMY_TIER` override | `shadow` (env override thừa) | gỡ bỏ, rơi về Redis cache `shadow` | ✅ cleaned |
| `omni_admin` schema/tenant | Bị báo cáo sai là "rỗng" (audit trước thiếu `\dn`) | Xác nhận đủ 19 bảng, migration đúng | ✅ không cần sửa schema |
| Tenant `staging-sim` (=3 VM lab) | Chưa provision → FK crash liên tục trong log onboarding | Đã tạo qua `AdminConfigRepo.create_tenant()` canonical | ✅ FIXED, hết FK error, row ghi thành công |
| brain-go role/image | Báo cáo audit trước claim mismatch | Xác minh: KHÔNG mismatch, false positive | ✅ không cần sửa |
| 8 Deployment `replicas=0` | Không phân loại | 5 xóa (RETIRED, manifest đã mất khỏi git từ `915e509`), 3 annotate giữ lại (STILL_CANONICAL, `deploy-siem-stack`) | ✅ FIXED |
| Restart gateway/brain-go | Chưa rõ nguyên nhân | Gateway: race Kafka-chưa-ready lúc startup (tự phục hồi). brain-go: graceful, trùng sự kiện hạ tầng chung ~13h trước | ✅ classified, không cần fix code |
| CLAUDE.md/MEMORY.md | Nói sai topology ("pod duy nhất") | Đã sửa: declared/deployed/retired rõ ràng | ✅ FIXED |
| pytest baseline | 5919 passed/1 flake (pre-existing) | Không chạy lại trong slice này (không đổi code Python) | không đổi |

## Trạng thái hiện tại
Repo: chỉ thay đổi tài liệu (`CLAUDE.md`, `docs/handoffs/CURRENT_SESSION.md`,
`docs/post-mortems/drift-correction-2026-07-02.md` mới, `k8s/services/omni-analyst-service.yaml`
đã `git rm`). **CHƯA COMMIT** — xem "Next step" bên dưới. Không có thay đổi source code Python nào
trong slice này (chỉ k8s runtime + docs).

## Đã hoàn thành (P0-1 → P0-5, P1 restart)
Xem bảng before/after trên + `docs/post-mortems/drift-correction-2026-07-02.md` cho chi tiết đầy đủ
từng bước, evidence, và bài học (đặc biệt: một agent con trong chuỗi audit trước tạo ra 1 finding
hoàn toàn sai — brain-go — do rối trong delegation lồng nhau; luôn tự verify lại bằng lệnh thật).

## Quyết định đã chốt (slice này)
- Runtime truth sửa trước, tài liệu chỉ cập nhật sau khi verify xong (không đảo ngược thứ tự).
- Không tạo bảng/schema ad hoc — dùng đúng `AdminConfigRepo.create_tenant()` đã có sẵn trong repo.
- Không xóa Deployment nào chỉ vì `replicas=0` — bắt buộc đối chiếu git history + PDB/Service trước.
- Không đổi Kafka partition/replication trong slice này (P1 riêng, không gây mất dữ liệu hiện tại).
- Không làm O2B/O2C trong slice này.

## Blockers
Không có blocker cho phần đã làm. VM/Agent truth trên 3 VM lab (cust-edge/cust-app/cust-db) vẫn
**BLOCKED** — audit trước thử SSH trực tiếp tới IP và thất bại, nhưng CHƯA thử đúng 2 cách canonical
của OrbStack (`orb -m <machine> <command>` hoặc `ssh <machine>@orb`) — kết luận BLOCKED trước đó
CHƯA đủ căn cứ, cần thử lại đúng cách trước khi kết luận lại.

## Next step chính xác — Iteration 3 của Continuous Productization Loop
Twin đã đủ 3/3 host (iteration 1+2 xong). **Bottleneck đã chọn sẵn cho iteration 3 (đừng chọn lại
từ đầu):** Operator visibility cho System Twin — hiện chỉ đọc được qua `redis-cli HGETALL` trực
tiếp, vi phạm Phase 11 Master Prompt ("Python function nội bộ không phải operator-visible"). Đây
là điểm đứt product chain kế tiếp: `Twin → Competency Matrix → operator visibility`.

Đề xuất cụ thể: kiểm tra `src/gateway/routes/onboarding.py` xem đã có route đọc Competency Matrix
chưa (O2A/O2B claim có +4 route competency/unknowns/questions/answer) — nếu có, verify chúng thật
sự đọc được `omni:aoip:system_model:staging-sim` bằng cách gọi API thật (không chỉ đọc code), test
end-to-end qua `curl` với `OMNI_GATEWAY_API_KEY`. Nếu route tồn tại nhưng chưa từng gọi thử thật,
đó cũng có thể là một deployment-drift/chưa-test-runtime khác giống iteration 1 — ĐỪNG giả định
route hoạt động chỉ vì code tồn tại.

Việc phụ (ưu tiên thấp hơn, làm nếu còn thời gian trong iteration 3 hoặc để dành iteration 4):
1. Sửa `agent:unknown` trong provenance (`src/aoip/onboarding_projection.py` — `to_observation()`
   không điền `agent_id` thật).
2. Thêm `OMNI_REMOTE_DISCOVERY_ENABLED=true` vào cơ chế provisioning VM mặc định (hiện không có
   trong `scripts/e2e_orbstack_fleet.py`) để VM mới không lặp lại gap giống cust-app.

Chỉ sau khi Twin 3/3 host + có operator visibility thật (không chỉ redis-cli) mới quay lại quyết
định O2B (source acquisition planner) — **theo đề xuất của user, golden journey
Tenant→Agent→Discovery→Fact→Twin→Competency phải chạy mượt và operator-visible trước khi mở rộng
sang Question/UnderstandingComplete**.

## Không được làm lại
- Không audit lại pipeline discovery→onboarding→SystemModel→CompetencyMatrix→Question từ đầu (đã
  map ở 3 commit O1/O2A/O2B trước đó — `1bc6292`/`cf9133f`/`c9cf0f7`).
- Không coi VM lab là BLOCKED — access method đúng là `orb -m <machine> <command>`, đã audit xong cả
  3 VM trong phiên này.
- Không tự động tạo lại Deployment `omni-analyst/core/executor/prober/worker` — đã xác nhận RETIRED
  qua git history, không phải xóa nhầm.
- Không revert `OMNI_AUTO_EXECUTE_ENABLED` về `true` mà không có yêu cầu tường minh mới từ user kèm
  cùng mức cẩn trọng như lần bật gốc (2026-06-11).
- Không giả định code local chưa deploy đúng chỉ vì runtime thiếu dữ liệu — LUÔN xác minh bằng
  `hasattr()`/`inspect.getsource()` trực tiếp trong pod trước khi sửa code (bài học iteration 1 —
  lỗi thực ra là deployment drift, không phải logic bug).
- Không tạo bảng/schema Postgres bằng SQL thủ công — canonical path đã có (`run_migrations()` +
  `AdminConfigRepo.create_tenant()`).

## Tài liệu liên quan
- `docs/post-mortems/drift-correction-2026-07-02.md` — chi tiết đầy đủ P0-1→P0-5 + P1, evidence,
  bài học.
- Memory `project_drift_correction_2026_07_02` — bản tóm tắt liên-session.
- `CLAUDE.md` mục "DEPLOYMENT STATE" (đã viết lại 2026-07-02) — topology declared/deployed/retired
  hiện tại, đọc trước khi giả định bất kỳ điều gì về hạ tầng.
- `k8s/deployments/omni-fullstack-autoexec-lab.yaml` — overlay lab kill-switch, đã revert qua đúng
  lệnh rollback tự ghi trong file này.
