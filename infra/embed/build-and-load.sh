#!/usr/bin/env bash
# Build omni-embed-cpu for OrbStack (linux/arm64). Same pattern as vllm build-and-load.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "==> Building omni-embed-cpu:latest (linux/arm64)"
docker buildx build \
  --platform linux/arm64 \
  -t omni-embed-cpu:latest \
  -f "${SCRIPT_DIR}/Dockerfile" \
  "${SCRIPT_DIR}"
echo "==> Done: omni-embed-cpu:latest"
echo "    kubectl apply -f infra/vllm/deployment-embedder.yaml"
