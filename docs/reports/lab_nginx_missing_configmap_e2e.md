# Lab E2E: nginx-test — ConfigMap không tồn tại (FailedMount)

## Mục tiêu

Một luồng duy nhất: **Pod nginx-test** không start được vì **volume mount ConfigMap** không có → điều tra qua **gateway → Kafka → omni-prober** → evidence có **FailedMount** / tên ConfigMap.

## Cách chạy

```bash
bash scripts/lab_nginx_test_missing_configmap_e2e.sh
```

Script: apply clean → patch Deployment (volume `broken-cfg` → `nginx-test-never-created-cm`) → **scale 0 / scale 1** (một pod, tránh RollingUpdate 2 pod + nhầm label) → `gateway_alert_loki_verify.sh` với `alertmanager_nginx_waiting_fault.json` và **`E2E_NGINX_POD_AUTO=1`** → restore Deployment.

## Kết quả đã kiểm tra (2026-04-09, lab OrbStack)

| Hạng mục | Kết quả |
|----------|---------|
| Gateway enqueue | `trace_id` trả về (vd. `gw-prom-f2bbae55a23a`, `gw-prom-d19e40829a52`) |
| Prober | `INGESTED` → `CONTEXT_READY` → `DIAGNOSED`; evidence `k8s_clinical_pod_events` raw chứa **`FailedMount` + `configmap "nginx-test-never-created-cm" not found`** |
| Pod label trong alert | **Bắt buộc** patch `labels.pod` = pod thật — dùng `E2E_NGINX_POD_AUTO=1`. Nếu `AUTO=0` và JSON còn placeholder → probe **404** (pod không tồn tại) |
| Analyst / Executor trong cửa sổ log ngắn | Không thấy dòng trace trong `deploy/omni-analyst` sau ~2 phút (consumer lag / cấu hình) — **cần** grep thêm sau `SLEEP_SEC` lớn hoặc chỉ consumer `omni-diagnostic-evidence` |

## Đánh giá

- **Điều tra phần prober thành công:** root cause **thiếu ConfigMap** được phản ánh trong **Pod events** (đúng với lỗi “configmap not found”).
- **Chưa đủ cho “full multi-agent”:** luồng **analyst → plan → executor → verify** cần xác nhận riêng (Kafka lag, hoặc `SLEEP_SEC` / log tail).

## Đề xuất tiếp

1. Tăng `SLEEP_SEC=180` và grep `omni-analyst` + `omni-executor` cho cùng `trace_id`, hoặc bật `E2E_ASSERT_DIAGNOSTIC_POLICY=1` khi đã có marker trong log.
2. (Tùy chọn) Bổ sung **`ContainerCreating`** vào tập “bad pod” picker trong `gateway_alert_loki_verify.sh` khi chỉ có một pod — hiện tại picker fallback `ready=False` đã đủ khi chỉ còn một pod lỗi.
3. Manual fix sau lab: tạo ConfigMap `nginx-test-never-created-cm` hoặc apply lại manifest sạch (script đã **restore** ở bước cuối).

## Trace mẫu để Loki

- `gw-prom-f2bbae55a23a` — run đầu (sau khi sửa `E2E_NGINX_POD_AUTO=1`).
- `gw-prom-d19e40829a52` — run script lab hoàn chỉnh.
