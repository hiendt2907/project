#!/usr/bin/env bash
# Theo dõi một trace_id trong log worker split (MPV3) hoặc omni-worker legacy.
# Usage: TRACE=... NS=multi-agent ./scripts/follow-trace.sh <trace_id>
# Env: FOLLOW_TRACE_DEPLOYS — override (space-separated), mặc định prober+analyst+core+executor[+worker nếu scale>0]
set -euo pipefail
NS="${NS:-multi-agent}"
TRACE="${1:?usage: $0 <trace_id>}"

KUBE="${KUBE:-kubectl}"
if [[ -x "$(dirname "$0")/with_working_kube.sh" ]]; then
  KUBE="$(cd "$(dirname "$0")" && pwd)/with_working_kube.sh"
fi

_default_deploys() {
  local base="omni-prober omni-analyst omni-core omni-executor"
  local r
  r="$("$KUBE" get deploy omni-worker -n "$NS" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
  if [[ "${r:-0}" != "0" ]]; then
    echo "omni-worker $base"
  else
    echo "$base"
  fi
}

DEPLOYS="${FOLLOW_TRACE_DEPLOYS:-$(_default_deploys)}"

for d in $DEPLOYS; do
  r="$("$KUBE" get deploy "$d" -n "$NS" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
  if [[ "${r:-0}" == "0" ]]; then
    continue
  fi
  echo "=== deployment/$d ===" >&2
  "$KUBE" logs -n "$NS" "deployment/$d" --tail=800 2>&1 | grep --line-buffered "$TRACE" || true
done
