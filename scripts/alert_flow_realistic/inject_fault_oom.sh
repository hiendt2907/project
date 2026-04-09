#!/usr/bin/env bash
# Patch memory limit rất thấp để container nginx OOM (kịch bản B — dùng cẩn thận).
# Khôi phục: ./inject_fault_restore.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"
NS="${FAULT_NS:-multi-agent}"
DEP="${FAULT_DEPLOY:-nginx-test}"
CONTAINER="${FAULT_CONTAINER:-nginx}"
# 8Mi thường không đủ cho nginx worker → OOMKilled
OOM_LIMIT="${OOM_MEMORY_LIMIT:-8Mi}"

echo "inject_fault_oom: ns=$NS deploy=$DEP memory_limit=$OOM_LIMIT"
"$KUBE" patch deployment "$DEP" -n "$NS" --type=strategic -p "{
  \"spec\": {
    \"template\": {
      \"spec\": {
        \"containers\": [{
          \"name\": \"${CONTAINER}\",
          \"resources\": {
            \"limits\": {\"memory\": \"${OOM_LIMIT}\"},
            \"requests\": {\"memory\": \"${OOM_LIMIT}\"}
          }
        }]
      }
    }
  }
}"
echo "OK — quan sát OOMKilled; restore: FAULT_NS=$NS FAULT_DEPLOY=$DEP $ROOT/scripts/alert_flow_realistic/inject_fault_restore.sh"
