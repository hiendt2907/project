# Replay payloads (Giai đoạn 2 — incident anonymized)

**Quy tắc:** Mỗi file `replay_*.json` phải **không** chứa secret, IP nội bộ, tên khách hàng. Chỉ dữ liệu đã strip + đổi tên generic.

## Định dạng

- Cùng schema Alertmanager webhook như [alertmanager_nginx_cpu_high.json](../alertmanager_nginx_cpu_high.json) (object có `alerts[]`, `receiver`, …).
- Thêm comment ngoài JSON không hợp lệ — dùng file này kèm **expected** trong README từng mẫu hoặc trong [artifact_template.json](../../../reports/alert-flow-realistic/artifact_template.json) khi chạy replay.

## Chạy

```bash
./scripts/alert_flow_realistic/post_gateway_alert.sh ./scripts/alert_payloads/replay/replay_example_minimal.json
```

Sau đó grep Loki / log worker theo `trace_id`.

## Mẫu

| File | Mục đích |
|------|----------|
| [replay_example_minimal.json](replay_example_minimal.json) | Ví dụ tối thiểu — thay labels bằng incident thật đã anonymize |
