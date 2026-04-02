# VictoriaMetrics → Prometheus

1. Tạo namespace (nếu chưa có): `kubectl apply -f k8s/monitor/namespace.yaml`
2. Apply stack mới: `prometheus.yaml`, `node-exporter.yaml`, `kube-state-metrics.yaml`, cập nhật `grafana.yaml`, `promtail.yaml`.
3. Kiểm tra: Prometheus targets (`/api/v1/targets`), Grafana datasource `http://prometheus:9090`.
4. Cập nhật Omni: ConfigMap `omni-worker-config` — `OMNI_PROMETHEUS_URL` / `OMNI_VMAGENT_URL` → `prometheus.monitor.svc.cluster.local:9090` (hoặc `http://…` đầy đủ), rollout `omni-worker`.
5. Gỡ cũ (mất dữ liệu TSDB VictoriaMetrics):

```bash
kubectl delete deployment victoria-metrics vmagent -n monitor --ignore-not-found=true
kubectl delete pvc victoria-metrics-data vmagent-data -n monitor --ignore-not-found=true
```

6. RBAC/ClusterRole cũ `vmagent-discovery` (nếu còn): `kubectl delete clusterrolebinding vmagent-discovery clusterrole vmagent-discovery --ignore-not-found=true`
