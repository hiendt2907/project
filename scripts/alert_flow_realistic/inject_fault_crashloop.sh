#!/usr/bin/env bash
# Patch deployment để container thoát non-zero → CrashLoopBackOff (kịch bản B).
# Khôi phục: ./inject_fault_restore.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"
NS="${FAULT_NS:-multi-agent}"
DEP="${FAULT_DEPLOY:-nginx-test}"
CONTAINER="${FAULT_CONTAINER:-nginx}"

echo "inject_fault_crashloop: ns=$NS deploy=$DEP container=$CONTAINER"
"$KUBE" patch deployment "$DEP" -n "$NS" --type=strategic -p "{
  \"spec\": {
    \"template\": {
      \"spec\": {
        \"containers\": [{
          \"name\": \"${CONTAINER}\",
          \"command\": [\"/bin/sh\"],
          \"args\": [\"-c\", \"sleep 2; exit 1\"]
        }]
      }
    }
  }
}"
echo "OK — đợi pod CrashLoop; sau đó POST alert hoặc để rule fire. Restore: FAULT_NS=$NS FAULT_DEPLOY=$DEP $ROOT/scripts/alert_flow_realistic/inject_fault_restore.sh"
