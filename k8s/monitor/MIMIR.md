# Grafana Mimir (monitor namespace)

- **Manifest:** `mimir.yaml` — monolithic single replica, PVC `mimir-data`, Service `mimir:9009`. Pod chạy non-root: bắt buộc `ruler.rule_path` trỏ vào volume (vd. `/data/mimir/data-ruler`), không để mặc định `./data-ruler/` trong image.
- **Remote write (Prometheus):** `http://mimir.monitor.svc.cluster.local:9009/api/v1/push` — configured in `prometheus.yaml` ConfigMap.
- **Grafana datasources:** `uid: prometheus` → Prometheus local `http://prometheus:9090` (Grafana Alerting + PromQL khớp scrape). `uid: mimir` → `http://mimir:9009/prometheus` (dashboard panels, dữ liệu remote_write). Không dùng Mimir cho unified alerting — remote_write trễ/thiếu mẫu so với TSDB local làm alert nhảy.
- **Rollback:** remove `remote_write` from Prometheus; xóa datasource Mimir hoặc gộp lại nếu cần; delete Mimir Deployment if needed.

Apply order: `mimir.yaml` → wait Ready → reload Prometheus (lifecycle or pod restart) → Grafana.
