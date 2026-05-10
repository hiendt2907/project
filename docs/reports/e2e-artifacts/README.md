# E2E artifacts (local / ticket)

Dùng thư mục này làm **gợi ý layout** khi lưu bằng chứng sau `scripts/gateway_alert_loki_verify.sh`, `scripts/e2e_one_alert_full_advisory_path.sh`, hoặc `scripts/e2e_collect_trace_evidence.sh`.

## Quy tắc

- **Không** commit token Telegram, cookie, hay payload có secret.
- File log / JSON Loki raw: ưu tiên đính kèm **ticket / PR** hoặc object store nội bộ; chỉ commit vào git nếu đã gỡ nhạy cảm.
- Đặt tên có `trace_id` và ngày: ví dụ `trace-gw-prom-xxxx-2026-05-05-evidence.txt`.

## Liên kết runbook

- [e2e_full_flow_evidence_checklist.md](../../runbooks/e2e_full_flow_evidence_checklist.md)
