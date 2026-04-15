# System contract — Omni Lab

Tóm tắt có kiểm chứng từ `docs/vendor/OMNI_PROJECT_CANONICAL.md` và `src/workers/settings.py`.

## Split topology (Master Plan V3)

- Một image worker (`multi-agent-system`), nhiều Deployment qua `OMNI_WORKER_ROLE`: `prober` | `analyst` | `core` | `executor` | `full` (monolith legacy).
- Gateway là **image riêng** (`omni-gateway`), **không** import worker/reasoning/executor.

## Kafka — luồng nghiệp vụ

1. Ingest → topic **`omni-alerts`** (gateway + prober đọc).
2. Prober → **`omni-diagnostic-evidence`** (analyst đọc).
3. Analyst → **`omni-actions`** (executor đọc).
4. Executor → **`omni-action-feedback`** (analyst đọc — **không** dùng topic `omni-results` trong code hiện tại).

Danh sách topic đầy đủ và script ensure: `scripts/kafka_ensure_omni_topics.sh`; tên mặc định khớp field `kafka_topic_*` trong `WorkerSettings`.

## Trace ID

- Mọi luồng nghiệp vụ giữ **một** `trace_id` từ ingest → probe → reasoning → execution → báo cáo.
- Gateway: client có thể gửi `X-Omni-Trace-Id` hoặc `?trace_id=` (định dạng hợp lệ); không thì sinh `gw-prom-…` (`src/gateway/api.py`).

## Worker roles (Kafka tóm tắt)

| `OMNI_WORKER_ROLE` | Vai trò chính |
|--------------------|----------------|
| `prober` | Alerts, delayed queue, circuit breaker; đưa evidence vào Kafka |
| `analyst` | Evidence + action feedback → actions (read-only mutate) |
| `core` | Deep scout, proactive, forecast (theo cấu hình) |
| `executor` | Chỉ consume `omni-actions`, thực thi mutation |
| `full` | Gộp (legacy) |

Chi tiết task background: `src/workers/omni_worker.py`.

## Redis vs Kafka

- **Kafka**: hàng đợi sự kiện giữa prober / analyst / executor.
- **Redis**: lock, circuit breaker / trace state, delayed ZSET, baseline snapshot — **không** dùng Redis List `BLPOP` làm worker queue chính (guardrail dự án).

## Đa cluster

- Một deployment Omni = một API Kubernetes (in-cluster hoặc một kubeconfig). **Không** có fleet registry đa cluster trong code — xem `project-memory` § TechnicalDebt.
