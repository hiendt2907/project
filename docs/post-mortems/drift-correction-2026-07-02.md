# Post-mortem: Runtime Drift Correction (2026-07-02)

## Bối cảnh
Một "Whole-System Autonomous SRE Reality Audit" (read-only) phát hiện runtime thật lệch khỏi
CLAUDE.md/MEMORY.md. Một Drift Correction Slice sau đó sửa runtime trước, xác minh lại, rồi mới
đồng bộ tài liệu — theo đúng nguyên tắc `Code = Deployment = Runtime = Documentation`.

## Phát hiện và xử lý

### P0-1 — Kill-switch bị bỏ quên (đã sửa)
`OMNI_AUTO_EXECUTE_ENABLED=true` trên Deployment `omni-fullstack`, override imperative từ phiên lab
`2090ac7` (2026-06-11) không bao giờ được rollback. Đã revert về `false` + `OMNI_SIEM_SUGGEST_ONLY=true`
+ gỡ override `OMNI_AUTONOMY_TIER` (rơi về Redis cache `shadow`, an toàn). Verify: pod mới nhận giá
trị đúng, health vẫn xanh (Kafka/Redis/LLM ok), không secret/configmap nào khác override.

### P0-2 — "DB rỗng" (root cause thật khác với báo cáo audit ban đầu)
Audit trước kết luận `omnidb` rỗng chỉ vì chạy `\dt` mà không `\dn` trước — bỏ sót toàn bộ schema
`omni_admin` (19 bảng, đã migrate đúng). Root cause thật: tenant `staging-sim` (chính là 3 VM lab
cust-edge/cust-app/cust-db) gửi evidence liên tục nhưng chưa được provision qua
`AdminConfigRepo.create_tenant()` → FK violation lặp lại trên `tenant_readiness_state`. Đã tạo
tenant qua đúng API canonical (không SQL ad hoc, không sinh migration mới). Verify: hết lỗi FK,
row `staging-sim` ghi thành công.

### P0-3 — brain-go role mismatch: FALSE POSITIVE
Audit ban đầu (qua một subagent bị rối trong chuỗi delegation) báo `omni-brain-go` có
`OMNI_WORKER_ROLE=onboarding`. Kiểm tra trực tiếp `kubectl get deployment -o yaml` cho thấy live
env chỉ có biến `BRAIN_*` (SIEM correlation), khớp đúng image `finguard/brain-go:siem-v2-corr`,
consumer group `brain-go-kafka` không trùng lặp. Không có mismatch, không sửa gì.

### P0-4 — 8 Deployment `replicas=0`
Phân loại bằng git history (không chỉ dựa `replicas=0`):
- `omni-analyst/core/executor/prober/worker`: manifest đã xóa khỏi git từ `915e509` (split-role
  consolidation) → RETIRED_REMOVE. Đã `kubectl delete` + xóa Service `omni-analyst` orphan đi kèm.
- `omni-siem-bridge/hitl-dispatcher/evidence-adapter`: manifest còn trong git (`replicas:1`,
  Makefile target `deploy-siem-stack` riêng, có PDB) → STILL_CANONICAL, chỉ đang tắt trong lab vì
  `omni-brain-go` đã đảm nhiệm SIEM correlation cho kịch bản hiện tại. Đã annotate
  `omni.io/status=scaled-down-intentional` + owner + sunset condition, không xóa.

### P1 — Restart-rate
`omni-gateway`: crash khởi động do race `KafkaConnectionError` (Kafka chưa sẵn sàng lúc pod start)
— dependency outage tự phục hồi, không phải bug logic. `omni-brain-go`: restart trước đó là
exitCode=0 graceful, trùng với một sự kiện hạ tầng chung khiến gần như toàn bộ pod restart đồng
loạt ~13h trước (nghi ngờ laptop/OrbStack VM sleep-wake) — không phải instability riêng lẻ.

## Chưa xử lý (ngoài scope slice này)
- Kafka `PartitionCount=1` trên mọi topic, không khớp doc "3 partitions" — P1 riêng, không gây mất
  dữ liệu hiện tại nên chưa sửa.
- VM/Agent truth trên 3 VM lab vẫn BLOCKED — cần khám phá đúng access method OrbStack (`orb -m`,
  `ssh <machine>@orb`) ở bước tiếp theo.

## Bài học
1. Luôn `\dn` trước `\dt` khi hệ thống dùng schema riêng ngoài `public`.
2. Không tin claim của agent con audit lồng nhau qua SendMessage nếu chưa tự verify lại bằng lệnh
   thật — một chuỗi delegation rối đã tạo ra ít nhất một finding hoàn toàn sai (P0-3).
3. `replicas=0` không đủ để kết luận một Deployment retired — phải đối chiếu git history + PDB/
   Service dependents.
4. Lab override (kill-switch, autonomy tier) áp dụng bằng `kubectl set env` phải có quy trình nhắc
   rollback — annotation/label/sunset condition ngăn nó bị bỏ quên qua nhiều tuần như lần này.
