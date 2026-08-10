# Backend Audit + Plan — 2026-08-10

Audit đọc-only (5 agent song song: silent-failure-hunter, security-reviewer,
database-reviewer, python-reviewer, Explore), phạm vi `src/` trừ `ui/` và 3
capability đã frozen cố ý (xem `PROACTIVE_FREEZE_2026-08-09.md`). Không sửa code
trong lúc audit. Mọi phát hiện dưới đây đã đọc code xác nhận, không suy đoán.

**Lưu ý va chạm:** phiên hiện tại đang có việc S3-S9 (proactive-first consolidation,
gate 24h) chạm `omni_worker.py`/`evidence_consumer.py`/`proactive_observer.py`. Một
số fix P0 dưới đây (#2, #3) CŨNG chạm các file này — làm tuần tự, không song song,
để tránh conflict và để mỗi thay đổi verify sống độc lập được.

## P0 — CRITICAL, làm ngay, verify sống bắt buộc trước khi coi là DONE

### #1 `/webhook/prometheus` không có API-key auth, fail-open khi thiếu HMAC secret
`src/gateway/api.py:583-681`, `_verify_hmac_signature()` dòng 153-166 trả `True` vô
điều kiện khi `OMNI_GATEWAY_WEBHOOK_SECRET` chưa set — không có fail-closed ở prod
như `_require_api_key` đang có (dòng 207-209). Nếu operator quên set secret ở prod,
endpoint nhận "Prometheus alert" mở hoàn toàn ra Internet, alert giả có thể đi thẳng
vào pipeline LLM/mutate.
**Fix:** thêm fail-closed check giống `_require_api_key` — nếu `OMNI_ENV_MODE=prod`
và `OMNI_GATEWAY_WEBHOOK_SECRET` rỗng → từ chối khởi động hoặc trả 503 mọi request.
**Verify sống:** deploy, thử gửi payload không HMAC vào endpoint qua `curl` từ ngoài
pod, xác nhận bị từ chối; xác nhận secret thật đang set trên GCP (đọc từ
`GCP_CREDENTIALS_2026-08-04.md`, không in ra).

### #2 Cổng `meta_self` có thể biến mất âm thầm, hai nơi đọc đều fail-open
Write: `src/workers/omni_worker.py:430-460` (`classify_alert()` trong try/except
debug-only). Read A: `evidence_consumer.py:750-764` (log debug khi lỗi). Read B:
`evidence_consumer.py:2236-2244` (`return None`, **không log gì cả**). Cả ba nơi coi
"key vắng mặt" = "không phải meta_self" = fail-open. Đang sống cùng lúc với
`OMNI_AUTO_EXECUTE_ENABLED=true` trên prod — đây là một trong số ít lá chắn chặn
Omni tự mutate dựa trên tiếng ồn do chính nó phát ra.
**Fix:** (a) nâng log write-failure và cả hai read-failure lên WARNING; (b) đảo
chiều fail-open→fail-closed: key vắng mặt vì lỗi phân loại → coi là "nghi meta_self,
chặn mutate" cho tới khi phân loại lại thành công, không phải ngược lại; (c) test
assert: `classify_alert` ném lỗi → alert không được auto-execute.
**Verify sống:** bơm alert giả có `alertname` gây `classify_alert()` ném lỗi (hoặc
mock có chủ đích), xác nhận log WARNING xuất hiện và mutate bị chặn, không phải
lọt qua.

### #3 `_AGENT_ONLINE_MAX_AGE_S=120` gõ cứng, độc lập với `OMNI_AGENT_COLLECT_INTERVAL`
`src/services/analyst/diagnosis_loop.py:46,447-461`. Đây là lần thứ **5** trong dự
án gặp đúng lớp bug "ngưỡng gõ cứng lệch khỏi config nó phụ thuộc" (trước:
`domain`, `signal_kind`, `_domain`, `snapshot_freshness`). Nếu operator tăng
`OMNI_AGENT_COLLECT_INTERVAL` (VM báo cáo thưa hơn), agent bị coi "offline" sai,
toàn bộ diagnosis loop rơi vào degraded mode âm thầm.
**Fix:** copy đúng pattern `snapshot_freshness_budget_sec()` đã dùng cho case #4 —
viết hàm suy `_AGENT_ONLINE_MAX_AGE_S` từ `OMNI_AGENT_COLLECT_INTERVAL` × hệ số an
toàn (đề xuất 2x, có sàn tối thiểu), viết test chặn việc gõ lại số cứng (như
`test_baseline_snapshot_freshness.py` đã làm).
**Verify sống:** đổi `OMNI_AGENT_COLLECT_INTERVAL` tạm thời trên 1 VM lab (hoặc mock
`last_seen` lệch giờ), xác nhận agent không bị coi offline sai trước ngưỡng mới.

## P1 — HIGH, làm ngay sau P0, cùng đợt

| # | Việc | File | Verify |
|---|---|---|---|
| 4 | Rate limit gateway là global, không per-tenant — 1 tenant ồn ào DoS được tenant khác | `src/gateway/api.py:317-330,600-610` | bắn traffic giả từ 1 API key, xác nhận key khác không bị ảnh hưởng sau fix |
| 5 | `psutil.cpu_percent(interval=1)` block cả event loop remote_agent 1s/chu kỳ | `src/remote_agent/collectors/system.py:39` | đo latency command-channel poll trước/sau trên VM lab |
| 6 | N+1 query trên đường HTTP `GET /competency/patterns` | `src/services/case_ledger/advocacy.py:104-121`, `src/gateway/routes/competency.py:71-106` | đếm số round-trip Postgres trước/sau qua log/tracing thật |
| 7 | `remote_agent_pipeline.py:211` hardcode `3.0` thay vì import `REMOTE_Z_THRESHOLD` | `src/anomaly/remote_host_baseline.py:30-32` là nguồn đúng | test regression chặn 2 giá trị trôi khỏi nhau |

## P2 — MEDIUM/MEDIUM-HIGH, cần quyết định thiết kế trước khi sửa (không vá vội)

| # | Việc | File | Ghi chú |
|---|---|---|---|
| 8 | `omni-executor-mutate-lab` ClusterRole cấp quyền Secrets **toàn cluster**, `INV_NAMESPACE_ISOLATION` chỉ enforce ở tầng app | `k8s/deployments/omni-fullstack-rbac.yaml:281-309` | cần đổi ClusterRole→Role theo từng namespace allowlist — RBAC thật, không phải vá code; cần xác nhận không phá ArgoCD sync trước khi đổi |
| 9 | `credential_source_of_truth` evidence gate tự-chứng-thực bởi chính LLM đề xuất mutation | `evidence_consumer.py:1065-1068`, `k8s_cluster_tools.py:1025` | governance-theater trên tool rủi ro cao nhất (`k8s_patch_secret`) — cần thiết kế xác minh độc lập, không chỉ đổi vài dòng |
| 10 | Mutate+audit không cùng transaction trong `identity_store.py` (khác `admin_config/repo.py` làm đúng) | `src/aoip/console/identity_store.py:73-143` | bọc `conn.transaction()` như `repo.py` đã làm — sửa nhanh, verify bằng chaos test ngắt kết nối giữa 2 câu lệnh |
| 11 | `baseline_promql_z_iops` đọc qua `getattr` nhưng **chưa từng khai báo** trong `Settings` — env var không bao giờ có tác dụng | `src/workers/baseline_snapshot.py:469-470` | thêm `Field` thiếu vào `settings.py`, thêm test chặn drift dạng này ở mọi field `baseline_promql_*` |
| 12 | Reconcile loop khởi động ghi tuần tự từng record, không batch | `src/services/agent_command_ledger/ledger.py:111-138` | chỉ ảnh hưởng readiness time lúc restart, không mất dữ liệu — ưu tiên thấp hơn |
| 13 | `REMOTE_WINDOW=60`/`REMOTE_TTL_SEC=7200` giả định `collect_interval` mặc định | `src/anomaly/remote_host_baseline.py:134-137` | cùng lớp bug #3 nhưng chưa gây hại thật (chỉ ảnh hưởng độ nhạy baseline, không silent-fail cứng) |
| 14 | Tenant-isolation `require_agent_tenant()` fail-open trên 2 nhánh lỗi | `src/gateway/tenant_context.py:85-100` | đổi fail-closed nhất quán với mọi cổng CRAT khác trong dự án |
| 15 | Task `_refill_tokens()` không có callback bắt lỗi | `src/gateway/api.py:399,324-329` | thêm `try/except` + log trong loop body, phòng regression tương lai |

## P3 — LOW, chỉ ghi nhận, không cần làm gấp

- #16 `lab_chaos_credential_autofix` bypass gate `required_evidence` hoàn toàn — hiện
  tắt mặc định (`lab_chaos_credential_autofix_enabled=False`), chỉ ghi nhận rủi ro
  thiết kế nếu pattern này được tái dùng cho playbook khác kém hẹp phạm vi hơn.
- #17 `list_patterns()`/`list_grants()` thiếu `LIMIT` — cardinality tự nhiên thấp,
  rủi ro dài hạn nếu alertname không chuẩn hoá.

## Xác nhận KHÔNG có vi phạm (đối chứng, đã kiểm kỹ — không cần sửa)

- `INV_GATEWAY_NO_WORKERS_IMPORT`: PASS, grep rỗng.
- `WRITE_VERBS`/`validate_command()`: PASS, mọi collector + executor dùng chung 1
  validator fail-closed, không đường nào bypass.
- `OMNI_EXECUTOR_FORCE_NSENTER` gate: PASS, đúng như thiết kế.
- CRAT fail-closed cho `ADVISORY_DISPATCHED`: PASS, `write_audit_block()` thật sự
  raise và chặn dispatch.
- Migration layer (14 file, `omni_admin`): PASS, không gap, không apply 2 lần.
- Index coverage cho hot-path chính: PASS.
- SQL injection: không tìm thấy, 100% parameterized.
- `domain_hint` bắt buộc khi gọi `detect_domain()`: PASS ở mọi call site thật tìm
  được (ngoài phạm vi 4 thư mục audit vòng Explore, nhưng đã kiểm chéo).
- RAG embed-once-per-turn: PASS, đúng pattern khuyến nghị.
- TODO/FIXME/XXX còn sót trong `anomaly/services/rag/pkg`: 0 kết quả.

## Thứ tự thực thi đề xuất

1. P0 #2 trước (an toàn nhất — không đụng chạm S3-S9 flag mới, chỉ nâng log +
   đảo fail-open→fail-closed trên đường đã có sẵn).
2. P0 #3 (độc lập, copy pattern đã có sẵn từ fix snapshot_freshness).
3. P0 #1 (độc lập, chỉ chạm gateway).
4. P1 theo thứ tự #7 → #6 → #5 → #4 (từ ít rủi ro nhất).
5. P2 cần bàn từng mục trước khi đụng vào (đặc biệt #8 RBAC — có thể phá ArgoCD
   sync nếu làm ẩu) — không tự ý làm khi chưa xác nhận.
6. P3 ghi vào backlog, không làm trong đợt này.

Mỗi mục P0/P1 khi DONE phải có bằng chứng verify sống (log/trace thật qua
`kubectl logs`/injection), đúng standing rule "cấm chỉ chạy pytest".
