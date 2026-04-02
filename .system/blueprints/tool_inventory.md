# Tool Inventory — Vũ khí & giới hạn (Capabilities)

Ánh xạ tới `workers/tools.py` và module con. **Cập nhật khi đăng ký tool mới.**

---

## Nguyên tắc chung

- Thực thi qua **JSON tool** do LLM phát ra; không chạy shell tự do trên process worker (trừ lab).
- **K8s:** chỉ **Kubernetes Python SDK** trong `k8s_tools.py` — không `subprocess kubectl` trong đường chuẩn.
- **Ollama:** mọi gọi LLM qua semaphore Redis `ollama_max_concurrent` khớp `OLLAMA_NUM_PARALLEL`; **luôn** `num_ctx=4096` (worker).

---

## Nhóm A — Observability & metrics

| Tool / vùng | Khả năng | Giới hạn |
|-------------|----------|----------|
| `promql_*`, `query_prometheus_metrics`, `query_historical_metrics`, dataframe/forecast | Đọc Prometheus | Timeout mạng; 403 → diagnosis; không dump raw TS vào prompt |
| `tool_audit_observability_stack` | Health Prometheus, targets, LGTM gợi ý | httpx + RBAC đọc pod |
| `metrics_exporter` (omni-worker) | Expose `/metrics` | Chỉ metrics worker, không thay Prometheus |

---

## Nhóm B — Trạng thái & bộ nhớ

| Tool / vùng | Khả năng | Giới hạn |
|-------------|----------|----------|
| Redis (session, streams) | Consumer group, XACK, DLQ pattern | Không dùng BLPOP |
| Qdrant | SOP search, experience, errors | Embed 768d; TTL semantic cache theo policy |

---

## Nhóm C — Kubernetes (Data Plane)

| Tool | Khả năng | Giới hạn |
|------|----------|----------|
| `k8s_list_pods`, `list_namespace_pods`, `list_all_pods_sdk` | Liệt kê pod | RBAC namespace / cluster theo RoleBinding |
| `k8s_rollout_restart` | Rollout restart | Thường **CONFIRM** Telegram trước khi ghi `write_pending` |

---

## Nhóm D — “Shell” & thực thi từ xa

| Cơ chế | Khả năng | Giới hạn |
|--------|----------|----------|
| `execute_in_sandbox` | Lệnh trong sandbox HTTP | Cần OpenSandbox deploy + URL |
| `gated_allowlisted_execute` | Sandbox + allowlist SDK | Validation chặt |
| `execute_shell_command` | Shell trên worker | **Chỉ** `OMNI_LAB_UNCHAINED` / god_mode — audit `audit:sandbox` |

---

## Nhóm E — Khác (Python / nội bộ)

| Tool | Khả năng | Giới hạn |
|------|----------|----------|
| `system_psutil` | CPU/RAM process | Chỉ host pod |
| `redis_health` | INFO, slowlog | Read-oriented |
| Charts (matplotlib) | PNG bytes | Gửi Telegram khi phù hợp |

---

## So với nhãn “Shell / Kubectl / Python Exec”

| Nhãn người dùng | Trên repo thực tế |
|-----------------|-------------------|
| Shell | → OpenSandbox HTTP hoặc lab shell có audit |
| Kubectl | → **SDK** (`kubernetes_asyncio`), không shell |
| Python Exec | → Tool async trong worker (không `eval` user arbitrary) |
