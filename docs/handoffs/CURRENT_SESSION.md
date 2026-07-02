# Current Session Handoff

## Deliverable hiện tại (phiên này) — 2 phần, cả hai HOÀN TẤT giai đoạn 1

### Phần 1: Runtime Drift Correction and Safety Restoration — HOÀN TẤT
Sau Whole-System Reality Audit phát hiện runtime lệch tài liệu, đã sửa P0 runtime trước, xác minh
lại, rồi đồng bộ tài liệu trong cùng slice. Chi tiết đầy đủ: `docs/post-mortems/drift-correction-2026-07-02.md`
+ memory `project_drift_correction_2026_07_02`.

### Phần 2: Continuous Productization Loop — Iteration 1 HOÀN TẤT (PARTIAL acceptance)
Bottleneck: System Twin (`omni:aoip:system_model:*`) trống hoàn toàn dù O1/O2A/O2B claim DONE.
**Root cause: deployment drift** — image `multi-agent-system:latest` chưa rebuild kể từ `1bc6292`
(pod thiếu hẳn `_project_into_system_model`, xác minh bằng `hasattr()` trực tiếp trong pod). Đã
`make docker-worker` + redeploy `omni-onboarding`+`omni-fullstack` → digest mới `c2d433daac77...`.
**Runtime proof:** Twin nay có dữ liệu thật, revision tăng 6→18, facts khớp VM truth cho 2/3 host
(cust-edge: nginx; cust-db: mariadbd:3306 + redis-server:6379). **cust-app CHƯA có discovery data**
(chỉ có metrics/log probe, không có process_list/port_scan/service_topology) — bottleneck kế tiếp,
CHƯA điều tra nguyên nhân. Chi tiết: `docs/product/PRODUCT_PROOF.md` (capability matrix + golden
journey status, mỗi dòng trỏ evidence cụ thể) + memory `project_productization_iteration1_twin`.

**Không commit code nào trong iteration 1** — root cause là deploy-only (không có diff source code,
code local đã đúng từ trước). Chỉ commit docs (PRODUCT_PROOF.md, CLAUDE.md, handoff).

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

## Next step chính xác — Iteration 2 của Continuous Productization Loop
**Bottleneck đã chọn sẵn cho iteration 2 (đừng chọn lại từ đầu):** điều tra vì sao Remote Agent trên
`cust-app` (192.168.139.237) không bao giờ emit probe loại discovery
(`process_list`/`port_scan`/`service_topology`/`nfs_topology`) — chỉ có `remote_system_metrics`/
`remote_log_errors`. Bằng chứng: `omni:evrl:p:staging-sim_cust-app:*` trong Redis chỉ có 2 key đó,
trong khi `cust-edge`/`cust-db` có đủ 4-5 probe loại discovery. Log agent trên cust-app
(`/var/log/omni-agent.log` qua `orb -m cust-app tail -f /var/log/omni-agent.log`) cho thấy
`POST .../webhook/agent/evidence "200 OK"` liên tục — network/gateway OK, vấn đề nằm ở phía
collector scheduling trong `src/remote_agent/` (có thể do config env khác biệt, ví dụ
`OMNI_AGENT_LOG_PATHS=/mnt/customer_logs/app.log` chỉ có trên cust-app — kiểm tra xem có flag nào
tắt discovery collector khi biến này được set).

Sau khi cust-app có discovery data đầy đủ (Twin 3/3 host), bottleneck tiếp theo nên là:
1. Sửa `agent:unknown` trong provenance (`src/aoip/onboarding_projection.py` — `to_observation()`
   không điền `agent_id` thật).
2. Operator visibility cho Twin (hiện chỉ đọc được qua `redis-cli` trực tiếp — vi phạm Phase 11 của
   Master Prompt "Python function nội bộ không phải operator-visible"). Có thể là API endpoint đơn
   giản `GET /onboarding/{tenant}/system-model` trước, ghi rõ PARTIAL nếu chưa có UI.
3. Chỉ sau khi Twin 3/3 host + có operator visibility mới quay lại quyết định O2B (source acquisition
   planner) — **theo đề xuất của user, golden journey Tenant→Agent→Discovery→Fact→Twin→Competency
   phải chạy mượt trước khi mở rộng sang Question/UnderstandingComplete**.

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
