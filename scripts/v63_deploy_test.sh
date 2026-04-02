#!/usr/bin/env bash
# Omni V6.3 Master Deployment & Chaos Test Script
set -e

echo "--- 🚀 INITIATING OMNI V6.3 RESILIENCE UPGRADE ---"

# 1. BUILD IMAGE
echo "[STEP 1] Building Docker image multi-agent-system:v6-chaos..."
docker build -t multi-agent-system:v6-chaos -f Dockerfile .
docker tag multi-agent-system:v6-chaos multi-agent-system:latest

# 2. DEPLOY K8S MANIFESTS
echo "[STEP 2] Deploying manifests (ConfigMap, Gateway, PromRules)..."
KUBECONFIG="${HOME}/.kube/config" kubectl --context=orbstack apply -f k8s/deployments/omni-worker-configmap.yaml
KUBECONFIG="${HOME}/.kube/config" kubectl --context=orbstack apply -f k8s/deployments/omni-prom-rules.yaml
KUBECONFIG="${HOME}/.kube/config" kubectl --context=orbstack apply -f k8s/deployments/omni-gateway.yaml

echo "[STEP 3] Updating Omni-Worker Deployment..."
KUBECONFIG="${HOME}/.kube/config" kubectl --context=orbstack apply -f k8s/deployments/omni-worker.yaml
KUBECONFIG="${HOME}/.kube/config" kubectl --context=orbstack rollout restart deployment/omni-worker -n multi-agent

# 3. VERIFICATION
echo "[STEP 4] Waiting for rollout status..."
KUBECONFIG="${HOME}/.kube/config" kubectl --context=orbstack rollout status deployment/omni-gateway -n multi-agent --timeout=60s
KUBECONFIG="${HOME}/.kube/config" kubectl --context=orbstack rollout status deployment/omni-worker -n multi-agent --timeout=60s

# 4. CHAOS TEST
echo "[STEP 5] RUNNING CHAOS SUITE V6.3..."
# Assuming Python 3.13 / ARM64 environment
python3 tests/chaos_suite.py

echo "--- ✅ OMNI V6.3 UPGRADE COMPLETED ---"
