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

**Iteration 3:** Chọn bottleneck "operator visibility cho Twin" — route `GET /onboarding/competency`
và `/unknowns` (O2A/O2B) đã tồn tại trong code nhưng gọi thật trả `500 Internal Server Error`.
**Root cause: cùng bug-class với iteration 1 (deployment drift) nhưng biến thể build-config** —
`Dockerfile.gateway` là Dockerfile RIÊNG cho gateway (minimal footprint, không copy toàn bộ `src/`),
và nó thiếu `COPY src/aoip/` — route import `aoip.question_lifecycle`/`aoip.competency_matrix` nên
`ModuleNotFoundError`. Xác nhận an toàn (grep không có `aoip` import `workers/`) rồi thêm 1 dòng
`COPY src/aoip/ /app/src/aoip/` vào `Dockerfile.gateway` + `make docker-gateway` + `make deploy-gateway`.
**Runtime proof:** gọi API thật với `Authorization: Bearer $OMNI_GATEWAY_API_KEY` →
`.../onboarding/competency?tenant_id=staging-sim&entity_type=host&entity_id=host:cust-app` trả
`identity: VERIFIED`, evidence_refs trỏ discovery thật; `.../onboarding/unknowns` trả Unknown thật.

**Đây là commit CODE thật đầu tiên trong loop** (`Dockerfile.gateway` +1 dòng) — khác iteration 1-2
vốn chỉ là deploy/config-only.

Chi tiết đầy đủ cả 3 iteration: `docs/product/PRODUCT_PROOF.md` (capability matrix + golden journey
status, mỗi dòng trỏ evidence cụ thể) + memory `project_productization_iteration1_twin` +
`project_productization_iteration2_custapp` + `project_productization_iteration3_gateway_aoip_import`.

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

## Next step chính xác — Iteration 4 của Continuous Productization Loop
Golden journey Tenant→Agent→Discovery→Fact→Twin→Competency đã chạy xuyên suốt VÀ operator-visible
qua API thật (iteration 1-3 xong). **Bottleneck đề xuất cho iteration 4 (chọn 1 trong các gợi ý,
không mở song song):**

1. **`agent:unknown` trong provenance** (`src/aoip/onboarding_projection.py` — `to_observation()`
   không điền `agent_id` thật vào Fact.provenance) — ảnh hưởng chất lượng bằng chứng của toàn bộ
   Twin, đáng làm sớm vì càng nhiều Fact tích lũy càng khó backfill.
2. **UX gap của `/onboarding/competency`**: `entity_id` yêu cầu format nội bộ `host:cust-app` thay
   vì `cust-app` — dễ gây operator hiểu nhầm "không có data" khi thực ra chỉ gọi sai tham số. Cân
   nhắc: chấp nhận cả 2 format ở route layer (không đổi domain logic).
3. **Provisioning gap `OMNI_REMOTE_DISCOVERY_ENABLED`**: `scripts/e2e_orbstack_fleet.py` không set
   biến này — nếu thêm VM thứ 4, sẽ lặp lại đúng gap iteration 2 đã fix thủ công.
4. Chưa kiểm tra Competency Matrix cho `cust-edge`/`cust-db` (chỉ mới verify `cust-app`) và chưa
   kiểm route `/onboarding/questions*` (O2B) có cùng lỗi `aoip` import hay không trên các route khác
   — **kiểm tra nhanh trước khi chọn bottleneck mới**: gọi thử `/onboarding/questions?tenant_id=staging-sim`
   để xác nhận không còn `ModuleNotFoundError` nào sót lại.

Gợi ý thứ tự: làm mục 4 trước (kiểm tra nhanh, có thể lộ thêm bug cùng loại), sau đó chọn 1 trong
mục 1-3 làm vertical slice chính cho iteration 4.

Chỉ sau khi các gap nhỏ này được dọn mới quay lại quyết định O2B (source acquisition planner) —
theo đề xuất của user, golden journey phải "sạch" trước khi mở rộng sang Question/UnderstandingComplete.

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
