#!/usr/bin/env bash
# Theo dõi một trace_id trong log omni-worker (vd: tg-123-456-789).
set -euo pipefail
NS="${NS:-multi-agent}"
TRACE="${1:?usage: $0 <trace_id>}"
kubectl logs -n "$NS" deployment/omni-worker --tail=800 2>&1 | grep --line-buffered "$TRACE" || true
