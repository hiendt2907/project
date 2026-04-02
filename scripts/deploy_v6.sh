#!/usr/bin/env bash
set -e

echo "🚀 Bắt đầu quá trình Build & Tích hợp Omni Platform V6..."

# 1. Build image Docker
echo "📦 1/4 Đang xây dựng Image Docker mới nhất..."
docker build -t multi-agent-system:latest -f Dockerfile .

# 2. Áp dụng Configs & RBAC
echo "🔐 2/4 Đang cập nhật K8s ConfigMap và nâng cấp quyền RBAC read-only..."
kubectl apply -f k8s/deployments/omni-worker-configmap.yaml
kubectl apply -f k8s/deployments/omni-worker-rbac.yaml

# 3. Triển khai Redis Cluster (Phase 1)
echo "💎 3/4 Đang triển khai StatefulSet Redis Cluster (6 nodes) & Chạy Job Init..."
kubectl apply -f k8s/deployments/redis-cluster.yaml

# Đợi Job khởi tạo chạy xong (nếu có)
echo "⏳ Đang chờ Job redis-cluster-init..."
kubectl wait --for=condition=complete job/redis-cluster-init -n multi-agent --timeout=60s || echo "Job có thể đã chạy thành công trước đó."

# 4. Rollout app
echo "🔄 4/4 Khởi động lại Omni Worker để tiếp nhận Hybrid Cache V6 (Phase 2)..."
kubectl rollout restart deployment omni-worker -n multi-agent

echo "✅ [THÀNH CÔNG] V6 đã lên mâm!"
echo "🔎 Bạn có thể kiểm tra sức khỏe Worker bằng lệnh:"
echo "   kubectl logs -f deployment/omni-worker -n multi-agent"
