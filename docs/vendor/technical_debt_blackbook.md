# Sổ đen — nợ kỹ thuật (mirror §13 Master Plan V3 review)

**Mục đích:** Ghi nhận các điểm nợ đã liệt kê tại `master_plan_v3_review_report.md` §13 + trạng thái xử lý. **Không** triển khai Rook/Ceph trong repo (đã bỏ khỏi scope).

| # | Nợ | Trạng thái / ghi chú |
|---|-----|----------------------|
| 1 | `omni-actions` / `omni-results` service | **omni-actions:** consumer `kafka_actions_loop`, Deployment `omni-executor`, topic trong `kafka_ensure_omni_topics.sh`. **omni-results** (reporter): chưa có. |
| 2 | `omni-core` + SA `cluster-admin` | Chưa thu hẹp — lab vận hành mutate. |
| 3 | Analyst runtime vs executor | **Đã tách:** `evidence_consumer` → `reason_diagnostic_evidence_only` (không `handle_inbound` / không `pkg.executor`). |
| 4 | Ollama Service DNS | Cảnh báo embed nếu không có `ollama-service` — chưa đổi. |
| 5 | Redis Sentinel | **Client:** `OMNI_REDIS_SENTINEL_HOSTS` + `OMNI_REDIS_SENTINEL_MASTER_NAME` → `redis.asyncio.sentinel`. **Cluster:** operator tự dựng Sentinel (xem `docs/vendor/redis_sentinel_lab.md`). |
| 6 | Grafana/Prometheus stack | Chưa mở rộng manifest monitor — tách khỏi MPV3 worker split. |

**Cập nhật:** 2026-04-02.
