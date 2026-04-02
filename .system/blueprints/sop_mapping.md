# SOP Mapping — Phân loại ký ức & “nhóm bệnh lý”

**Qdrant** không có “10.000” cố định — đây là **khung phân loại** để ingest SOP và truy vấn semantic (collection `itops_sop_ledger`, embed 768 chiều).

---

## 1. Trục phân loại (taxonomy)

| Nhóm | Dấu hiệu / từ khóa gợi ý | PromQL / signal gợi ý |
|------|--------------------------|------------------------|
| **CPU** | Throttle, high load, steal time | `rate(container_cpu_*`, `node_cpu_seconds_total` |
| **RAM / OOM** | OOMKilled, working set, cache pressure | `container_memory_*`, cgroup, `node_memory_*` |
| **Network** | Timeout, packet drop, DNS | `node_network_*`, probe fail, ingress 5xx |
| **Disk / IO** | Eviction, volume full, iowait | `node_disk_*`, PVC usage (exporter phụ thuộc) |
| **App / runtime** | CrashLoop, probe fail, 5xx | kube pod status, app metrics job |
| **Data plane** | Redis slow, Qdrant latency | redis_exporter, custom metrics |
| **Control / rollout** | Bad rollout, ImagePull | deployment replicas, events |

---

## 2. Cách “gán” vào Qdrant

1. Mỗi SOP = một (hoặc vài) **điểm** vector với payload:
   - `category`: một trong các nhóm trên (+ tag phụ: `language`, `severity`).
   - `title`, `body`: nội dung procedure.
   - `source`: seed YAML / runbook id.
2. Truy vấn: embed câu hỏi user hoặc **chuỗi chữ ký lỗi** (error ledger) → nearest neighbors.
3. **Action experience** (`action_experience`): ghi nhận cặp (context → tool thành công) để fast-path sau này.

---

## 3. Không trộn lẫn

- **SOP** = quy trình đúng (runbook).
- **Error ledger** = sự cố đã xảy ra (tách collection `itops_error_ledger`).
- **CLI HIL** = ngữ cảnh gợi ý read-only cho human — không dùng làm SOP tier-1.

---

## 4. Mở rộng

- Thêm nhóm **Security / RBAC** nếu ingest policy audit.
- Đồng bộ tag với `primary_bucket_for_metrics` trong `slow_path_trace` khi refactor.
