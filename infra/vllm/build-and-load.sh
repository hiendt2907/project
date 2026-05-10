#!/usr/bin/env bash
# build-and-load.sh — Build vLLM image for Orbstack (Apple M4).
#
# Orbstack shares the Docker image cache with its K8s cluster natively.
# No registry push is needed — imagePullPolicy: Never in the manifest
# forces K8s to use the locally built image directly.
#
# Usage:
#   ./infra/vllm/build-and-load.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Building vllm-m4:latest for linux/arm64 (Orbstack / Apple M4)"
docker buildx build \
  --platform linux/arm64 \
  -t vllm-m4:latest \
  -f "${SCRIPT_DIR}/Dockerfile" \
  .

echo "==> Build complete: vllm-m4:latest"
echo "    Apply manifests:"
echo "      kubectl apply -f infra/vllm/deployment.yaml"
