#!/usr/bin/env bash
# Product release gate: repository-level proof before a production rollout.
# Cluster-only drills (pre-deploy-validate/e2e-portal/chaos) remain separate gates.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> diff hygiene"
git diff --check

echo "==> architecture boundaries + runtime safety"
.venv/bin/python -m pytest \
  tests/test_runtime_layer_boundaries.py \
  tests/test_aoip_operations.py \
  tests/test_gateway_agent_runtime.py \
  tests/test_aoip_delivery_loop.py -q --tb=short

echo "==> full unit regression"
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration --ignore=tests/real_services

echo "==> provider/tenant portal builds"
(cd ui/apps/provider-portal && npm run build)
(cd ui/apps/tenant-portal && npm run build)

echo "PRODUCT RELEASE GATE: PASS"
echo "Cluster proof remains mandatory: make pre-deploy-validate e2e-portal"
