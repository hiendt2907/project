#!/usr/bin/env bash
# Rollback deployment về revision trước (sau fault injection).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"
NS="${FAULT_NS:-multi-agent}"
DEP="${FAULT_DEPLOY:-nginx-test}"

echo "inject_fault_restore: rollout undo $DEP -n $NS"
"$KUBE" rollout undo "deployment/$DEP" -n "$NS"
"$KUBE" rollout status "deployment/$DEP" -n "$NS" --timeout=120s
echo "OK"
