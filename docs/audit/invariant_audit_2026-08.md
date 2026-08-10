# Invariant Audit — đối chiếu CLAUDE.md vs code thật (2026-08-10, Đ49 B0)

Đối chiếu trực tiếp bằng grep code thật (`src/`) + test thật (`tests/`), không
suy đoán từ tài liệu. Phạm vi: các INV_*/ERR_* liệt kê trong mục INVARIANTS
của `CLAUDE.md`.

| Invariant | Enforce ở đâu (file:line) | Test bảo vệ | Trạng thái |
|---|---|---|---|
| `INV_KNOWLEDGE_NOT_ALERT` | `src/gateway/routes/agent_webhook.py:109` (routing), `src/workers/knowledge_pipeline.py:3`, `src/workers/omni_worker.py:909` | `tests/test_knowledge_pipeline.py` | đúng |
| `INV_DATA_RESIDENCY` | `src/remote_agent/collectors/discovery_evidence.py:3,215` (hash-on-host) | `tests/test_customer_knowledge_context.py` | đúng |
| `INV_PUBLIC_PLANE_ISOLATED` | `src/aoip/console/oidc.py:101-102` (`claims.get("iss") != cfg.issuer` → raise) + `cloudflare/tunnel/verify.sh` nhóm A (so issuer lab/public không đổi qua CI) | `tests/test_aoip_oidc.py` | đúng — **không dùng literal `INV_PUBLIC_PLANE_ISOLATED` làm marker trong code** (0 hit `grep -rn` trong `src/`/`tests/`), enforce bằng so sánh cấu hình + verify.sh, không phải constant như các invariant khác. Không phải bug, nhưng khác pattern (các invariant khác đều có `reason_codes.py` constant) — nếu muốn nhất quán có thể thêm comment marker, KHÔNG bắt buộc. |
| `INV_NO_RESTART_ON_BROKEN_SPEC` | `src/workers/evidence_consumer.py:26,1708-1710` | `tests/test_cov_pkg_diagnostic_evidence_build.py`, `tests/test_configmap_remediation.py` | đúng |
| `INV_READ_BEFORE_MUTATE` | `src/pkg/reasoning/reason_codes.py:23-24`, dùng trong `src/workers/evidence_consumer.py:27` | `tests/test_cov_pkg_diagnostic_evidence_build.py` | đúng |
| `INV_NAMESPACE_ISOLATION` | `src/workers/evidence_consumer.py:25,2078` | `tests/test_mutate_namespace_isolation.py`, `tests/test_kpi_no_data_not_zero.py` | đúng |
| `ERR_REA_NO_PHYSICAL_PROOF` | `src/pkg/reasoning/reason_codes.py:15`, dùng ở `src/workers/evidence_consumer.py:854` | `tests/test_cov_pkg_reason_embed_evidence.py`, `tests/test_domain_lane_boundary.py`, `tests/test_f22_hoisted_safety_net.py` | đúng |
| `ERR_GOV_UNAUTHORIZED_MUTATION` | `src/pkg/reasoning/reason_codes.py`, dùng ở `src/workers/kafka_actions_consumer.py:653`, `src/workers/autonomous_execute.py:15` | `tests/test_cov_autonomous_execute.py`, `tests/test_auto_execute_gate.py`, `tests/test_track1b_worker_kafka.py` | đúng |

## Ghi chú

- 8/8 invariant kiểm tra đều có enforce + test thật trong code hiện tại —
  không phát hiện invariant nào bị bỏ rơi hoàn toàn ("không tìm thấy
  enforce"). Đây là kết quả TỐT, không phải bỏ sót audit — B0 chỉ nhằm xác
  nhận nền tảng trước khi vào B1-B6 (vốn đã có bằng chứng lệch cụ thể từ
  memory audit trước, không nằm trong 8 invariant "core" này).
- Không audit toàn bộ danh sách phụ (`OMNI_EXECUTOR_FORCE_NSENTER`,
  `MUTATE_TOOL_ALLOWLIST`, các gate autonomy tier...) trong lượt B0 này —
  phạm vi giữ đúng 8 invariant chính đã liệt kê tường minh trong CLAUDE.md
  mục INVARIANTS để không lan man ngoài blueprint gốc.

---

## B1 — `hitl_decision` dead path: verify sống trên GCP UAT (2026-08-10)

Root-cause commit đã merge vào `main` từ lâu (`d9f7278` #27, `b802a66` #28,
`383cc1a` #29/#30 — cả 3 xác nhận `git merge-base --is-ancestor` đều nằm
trong lịch sử HEAD hiện tại, không còn "local-only" như memory 2026-08-04
ghi).

**Verify sống qua `kubectl exec omni-postgres-0` (schema `omni_admin`, 32
bảng xác nhận đúng số lượng CLAUDE.md ghi):**

| Bảng | Số dòng | Ghi chú |
|---|---|---|
| `hitl_decision` | **0** | `min(created_at)`/`max(created_at)` đều NULL — 0 dòng tuyệt đối, không phải do lọc thời gian |
| `case_ledger` | 0 | |
| `advisory_acknowledgment` | 0 | |
| `agent_command_outcome` | 0 | |
| `autonomy_tier_history` | 0 | |
| `config_change_log` | 7 | có hoạt động portal thật |
| `crat_outbox` | 7 | |
| `portal_auth_audit` | 5 | |
| `tenant` | 2 | |

**Đánh giá:** Không thể kết luận "vẫn chết" hay "đã hết chết" — DB có hoạt
động portal thật (login/config change) nhưng KHÔNG có dòng nào ở 5 bảng
liên quan diagnostic/mutation/HITL. Log `omni-fullstack` (pod mới restart
2026-08-10T10:23Z do build #49, chỉ còn ~2 phút log) cho thấy 1 sự kiện
`advisory_escalation_tier tier=L2_SUGGEST verdict=INVESTIGATE` — tức có
traffic thật đi qua tầng advisory, nhưng tier quan sát được chỉ dừng ở
L2_SUGGEST, chưa từng thấy `L3_HITL` trong log hiện có (log quá ngắn để
kết luận chắc — pod vừa restart).

**Kết luận thận trọng:** Nhiều khả năng đây là "chưa có sự cố nào chạm
đúng ngưỡng L3_HITL kể từ khi hạ tầng chuyển sang GCP (2026-08-04)" chứ
không chắc là "code vẫn nuốt tín hiệu" — 2 giả thuyết này KHÔNG phân biệt
được chỉ bằng đọc dữ liệu tĩnh, cần 1 sự kiện HITL thật (qua Admin
Simulator `/simulate/{lane}` đã có sẵn, không phải tính năng mới) để xác
nhận dứt điểm. Đây là next step cụ thể, chưa thực hiện trong lượt này vì
sinh ra 1 lần escalation thật sẽ gửi Telegram thật tới admin — cần user
xác nhận trước khi kích hoạt trên UAT.

---

## B2 — Vòng học chỉ nhận nhãn khen: ĐÃ FIX từ trước (2026-08-10)

Memory `project_learning_loop_broken_labels` (2026-07-30) mô tả: verdict
`None` (callback 1-nút cũ) bị coi là "đồng ý", verdict `INCORRECT` ("Sai")
chỉ bị bỏ qua, không ghi tín hiệu âm nào vào `omni:kpi:z:*:false_positive`.

**Xác nhận qua code thật (`src/workers/advisory_ack.py:296-326`)**: đây là
đúng bug đã mô tả, nhưng đã được sửa bởi commit `383cc1a` ("#29/#30 nút Sai
không ghi false_positive; callback cũ bị coi là đồng ý") — commit này nằm
trong lịch sử `main` hiện tại (xác nhận `git merge-base --is-ancestor`).
Code hiện tại:
- `verdict is None` (callback 1 nút cũ) → không ghi gì cả, tường minh
  (dòng 309-312 có comment giải thích chính xác lý do).
- `verdict == VERDICT_INCORRECT` → gọi `store.record_false_positive()`
  (dòng 321-322), không còn bị bỏ qua.
- `_record_case_verdict()` ghi verdict thật vào `case_ledger` qua
  `store.record_verdict(diagnosis=verdict)`, trả `pattern_key` đã đóng băng
  từ lúc mở ca — vòng học pattern dùng đúng nhóm.

Có test hồi quy chuyên biệt:
`tests/test_advisory_ack.py::test_incorrect_verdict_records_false_positive_not_accepted`.

**Kết luận:** Không cần sửa thêm. Đóng B2 — bằng chứng đủ mạnh (code +
comment giải thích rõ + test hồi quy đặt tên đúng theo bug), không cần
trigger sống thêm trên UAT (khác B1, đây không phải đường phụ thuộc cluster
runtime, logic thuần và đã có test bảo vệ).
