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
