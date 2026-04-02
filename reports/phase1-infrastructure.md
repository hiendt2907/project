# Chặng 1 — Hạ tầng K8s (nghiệm thu)

## Phạm vi

- `k8s/monitor/victoria-metrics.yaml`: namespace `monitor`, PVC, Deployment single-node, Service `:8428`.
- `k8s/deployments/namespace.yaml`: namespace `multi-agent`.
- `k8s/deployments/redis.yaml`: PVC, Redis 7 + AOF, Service `redis:6379`.
- `k8s/deployments/qdrant.yaml`: PVC, Qdrant, Service HTTP `6333` / gRPC `6334`.
- `requirements.txt`: thư viện Python cho các chặng sau.

## Kiểm thử đề xuất (bạn chạy local)

```bash
# Áp namespace + workloads
kubectl apply -f k8s/monitor/victoria-metrics.yaml
kubectl apply -f k8s/deployments/namespace.yaml
kubectl apply -f k8s/deployments/redis.yaml
kubectl apply -f k8s/deployments/qdrant.yaml

# Trạng thái Pod (mong đợi Running + READY 1/1)
kubectl get pods -n monitor
kubectl get pods -n multi-agent

# VictoriaMetrics health
kubectl run -n monitor curl --rm -it --restart=Never --image=curlimages/curl -- \
  curl -sS http://victoria-metrics.monitor.svc:8428/health

# PromQL API (instant query ví dụ)
kubectl run -n monitor curl --rm -it --restart=Never --image=curlimages/curl -- \
  curl -sS 'http://victoria-metrics.monitor.svc:8428/api/v1/query?query=up'

# Redis
kubectl exec -n multi-agent deploy/redis -- redis-cli ping

# Qdrant
kubectl run -n multi-agent curl --rm -it --restart=Never --image=curlimages/curl -- \
  curl -sS http://qdrant.multi-agent.svc:6333/readyz
```

## Môi trường Python

```bash
cd /path/to/project
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip check
```

## Lỗi gặp phải (ledger)

| Thời điểm | Thành phần | Mô tả | Cách xử lý |
|-----------|------------|-------|------------|
| _(trống)_ | — | Chưa có lỗi khi viết manifest | — |

**Ghi chú:** Ghi nhận lỗi runtime vào collection Qdrant (`itops_sop_ledger` / ledger lỗi) được lên lịch từ **Chặng 2** khi có `qdrant-client` và module khởi tạo collection.

## Tiêu chí nghiệm thu Chặng 1

- [ ] `kubectl get pods -n monitor` — `victoria-metrics-*` Running.
- [ ] `kubectl get pods -n multi-agent` — `redis-*`, `qdrant-*` Running.
- [ ] PVC `Bound` cho cả ba workload (nếu cluster có StorageClass mặc định).
- [ ] `pip install -r requirements.txt` thành công.
