#!/usr/bin/env bash
# In ra cluster-info của cluster đã chọn (probe giống with_working_kube.sh).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/lib/kube_resolve.sh"

if ! line="$(kube_resolve_probe)"; then
  echo "FAIL: không có cluster kubectl nào trả lời." >&2
  exit 1
fi

IFS=$'\t' read -r kcfg ctx <<<"$line"
if [[ "$kcfg" == "__DEFAULT__" ]]; then
  unset KUBECONFIG
else
  export KUBECONFIG="$kcfg"
fi
args=()
[[ -n "$ctx" ]] && args=(--context "$ctx")
kubectl "${args[@]}" cluster-info --request-timeout=8s
echo "OK — KUBECONFIG=${KUBECONFIG:-<unset>}${ctx:+  --context=$ctx}"
