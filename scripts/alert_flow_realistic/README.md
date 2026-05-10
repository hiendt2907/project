# Alert flow realistic — fault injection & gateway POST (kịch bản B)

**Phạm vi:** Hỗ trợ [alert-flow-realistic-test-plan.md](../../docs/reports/alert-flow-realistic-test-plan.md) giai đoạn 1 — không thay smoke case A.

## Biến môi trường chung

| Biến | Mặc định | Ý nghĩa |
|------|----------|---------|
| `FAULT_NS` | `multi-agent` | Namespace deployment bị fault |
| `FAULT_DEPLOY` | `nginx-test` | Deployment lab ([scripts/nginx-test-deployment.yaml](../nginx-test-deployment.yaml)) |
| `FAULT_CONTAINER` | `nginx` | Tên container trong pod template |

Prefix kubectl: `./scripts/with_working_kube.sh` (OrbStack / kubeconfig).

## Script

| Script | Tác dụng |
|--------|----------|
| [inject_fault_crashloop.sh](inject_fault_crashloop.sh) | Patch container chạy `exit 1` → CrashLoop (SDK/events khớp) |
| [inject_fault_oom.sh](inject_fault_oom.sh) | Hạ `memory` limit xuống rất thấp → OOMKilled (cẩn thận node) |
| [inject_fault_restore.sh](inject_fault_restore.sh) | `kubectl rollout undo` — khôi phục revision trước |
| [post_gateway_alert.sh](post_gateway_alert.sh) | POST file JSON Alertmanager-style → `omni-gateway` (giống E2E). **Bắt buộc** `NS=<ns>` (vd. `NS=multi-agent ./post_gateway_alert.sh path.json`). |

Sau khi inject: đợi rule Prom fire hoặc POST payload tương ứng pod/labels; grep `trace_id` trên prober/analyst/executor; điền [artifact_template.json](../../reports/alert-flow-realistic/artifact_template.json).

## An toàn

- Chỉ namespace lab/staging đã duyệt.
- Restore ngay sau khi thu log xong.
- OOM script có thể restart pod nhiều lần — dùng quota hợp lý.
