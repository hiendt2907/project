# RFC: Omni multi-cluster (ngắn)

## Bối cảnh

Một **trace_id** nghiệp vụ phải giữ nguyên qua ingest → Kafka → worker → executor → feedback. Multi-cluster mở rộng vận hành mà không nhân đôi identity logic.

## Phương án

| Cách | Mô tả | trace_id |
|------|--------|----------|
| A | Một control plane, nhiều kubeconfig / context — analyst chọn cluster theo label hoặc ConfigMap | Không đổi; thêm `cluster_id` trong labels nếu cần RAG filter |
| B | Nhiều deployment Omni độc lập (mỗi cluster một stack) — không share Redis/RAG | trace_id local per cluster; không merge cross-cluster |
| C | Hub: gateway tập trung, worker shard theo topic prefix | trace_id global; cần routing key Kafka |

## Khuyến nghị lab

- Bắt đầu **B** (stack độc lập) để giảm coupling.
- Khi cần một dashboard: dùng biến Grafana `cluster` hoặc Loki label `namespace` + `cluster_id` (sau khi chuẩn hoá label schema).

## Rủi ro

- Trùng **trace_id** nếu generator không UUID — giữ UUID hoặc prefix cluster.
- RAG pgvector: experience không nên trộn workload giữa cluster prod khác nhau trừ khi cố ý; dùng collection hoặc filter `cluster_id`.

## Việc tiếp theo (ngoài scope sprint)

- Gate schema drift (`data-e2e-single-format` trong plan).
- Document `OMNI_*` per profile trong ConfigMap mẫu production-like.
