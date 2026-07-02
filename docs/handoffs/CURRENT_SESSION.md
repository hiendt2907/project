# Current Session Handoff

## Deliverable hiện tại (phiên này) — HOÀN TẤT
**Runtime Drift Correction and Safety Restoration** — sau Whole-System Reality Audit phát hiện
runtime lệch tài liệu, đã sửa P0 runtime trước, xác minh lại, rồi đồng bộ tài liệu trong cùng slice.
Chi tiết đầy đủ: `docs/post-mortems/drift-correction-2026-07-02.md` + memory
`project_drift_correction_2026_07_02`.

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

## Next step chính xác
1. **Commit checkpoint cho slice này** (chưa làm — làm ngay đầu session sau nếu chưa commit):
   `git add CLAUDE.md docs/handoffs/CURRENT_SESSION.md docs/post-mortems/drift-correction-2026-07-02.md`
   + `git rm` đã stage sẵn cho `k8s/services/omni-analyst-service.yaml` — commit message gợi ý:
   `fix(runtime): restore safe canonical omni deployment state`. Lưu ý: các thay đổi K8s (kubectl
   delete/set env/annotate) đã áp dụng trực tiếp lên cluster, KHÔNG có file manifest tương ứng để
   commit cho phần kill-switch/annotation (đó là live cluster state, không phải source-controlled) —
   chỉ commit phần docs + xóa manifest orphan.
2. **Tiếp tục Whole-System Reality Audit — Phần C/D/F (VM truth)**: khám phá đúng access method
   OrbStack theo đúng thứ tự: `orb status` → `orb list` → `orb info <machine>` → thử
   `orb -m <machine> hostname` → nếu lỗi mới thử `ssh <machine>@orb hostname` → chỉ kết luận BLOCKED
   nếu CẢ HAI đều thất bại. Sau khi có access, audit VM truth (hostname/IP/route/listener/service),
   Remote Agent runtime trên từng VM, rồi so sánh với System Twin (Fact/SystemModel/Competency
   Matrix) cho tenant `staging-sim` (nay đã được provision đúng cách, có thể dùng làm case study).
3. Chỉ sau khi VM/Twin audit xong và không phát hiện gap nghiêm trọng mới, mới quay lại quyết định
   O2B (source acquisition planner) hay tiếp tục hardening theo phát hiện VM/Twin — **theo đề xuất
   của user, ưu tiên VM/Twin validation trước O2B** vì hiện có Fact→SystemModel→Competency Matrix
   nhưng chưa chứng minh chúng phản ánh đúng 3 VM thật.

## Không được làm lại
- Không audit lại pipeline discovery→onboarding→SystemModel→CompetencyMatrix→Question từ đầu (đã
  map ở 3 commit O1/O2A/O2B trước đó — `1bc6292`/`cf9133f`/`c9cf0f7`).
- Không coi kết luận "BLOCKED — không SSH được VM" của audit lần trước là cuối cùng — đó là do thử
  sai access method (SSH thẳng tới IP), CHƯA thử `orb -m`/`ssh ...@orb`.
- Không tự động tạo lại Deployment `omni-analyst/core/executor/prober/worker` — đã xác nhận RETIRED
  qua git history, không phải xóa nhầm.
- Không revert `OMNI_AUTO_EXECUTE_ENABLED` về `true` mà không có yêu cầu tường minh mới từ user kèm
  cùng mức cẩn trọng như lần bật gốc (2026-06-11).
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
