#!/usr/bin/env bash
# LEGACY MONOLITH SCRIPT: kept for historical comparison only.
# Preferred split deployment flow: `make deploy-worker`.
set -e

echo "🚀 Bắt đầu quá trình Build & Tích hợp Omni Platform V6..."

# 1. Build image Docker
echo "📦 1/4 Đang xây dựng Image Docker mới nhất..."
docker build -t multi-agent-system:latest -f Dockerfile .

# 2. Áp dụng Configs & RBAC
echo "🔐 2/4 Đang cập nhật K8s ConfigMap và nâng cấp quyền RBAC read-only..."
kubectl apply -f k8s/deployments/omni-worker-configmap.yaml
kubectl apply -f k8s/deployments/omni-worker-rbac.yaml

# 3. Redis standalone (AOF + PVC) — Service DNS `redis:6379`
echo "💎 3/4 Đang triển khai Redis standalone..."
kubectl apply -f k8s/deployments/redis-standalone.yaml
echo "⏳ Đang chờ StatefulSet redis Ready..."
kubectl rollout status statefulset/redis -n multi-agent --timeout=120s || true

# 4. Rollout app
echo "🔄 4/4 Khởi động lại Omni Worker để tiếp nhận Hybrid Cache V6 (Phase 2)..."
kubectl rollout restart deployment omni-worker -n multi-agent

echo "✅ [THÀNH CÔNG] V6 đã lên mâm!"
echo "🔎 Bạn có thể kiểm tra sức khỏe Worker bằng lệnh:"
echo "   kubectl logs -f deployment/omni-worker -n multi-agent"
