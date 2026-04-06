#!/usr/bin/env bash
# Drop-in thay cho kubectl (KHÔNG gõ thêm "kubectl" phía trước):
#   ./scripts/with_working_kube.sh get ns
#   ./scripts/with_working_kube.sh apply -f ...
# Nếu lỡ gõ kubectl, script tự bỏ token thừa:
#   ./scripts/with_working_kube.sh kubectl get ns  →  kubectl … get ns
# Probe: OrbStack → ~/.kube mặc định → $KUBECONFIG → merge mặc định.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/lib/kube_resolve.sh"

if [[ "${1:-}" == "kubectl" ]]; then
  shift
fi

if ! line="$(kube_resolve_probe)"; then
  echo "FAIL: không có cluster kubectl nào trả lời (OrbStack, ~/.kube, \$KUBECONFIG, default)." >&2
  exit 1
fi

IFS=$'\t' read -r kcfg ctx <<<"$line"

if [[ "$kcfg" == "__DEFAULT__" ]]; then
  unset KUBECONFIG
  exec kubectl "$@"
fi

export KUBECONFIG="$kcfg"
if [[ -n "$ctx" ]]; then
  exec kubectl --context="$ctx" "$@"
fi
exec kubectl "$@"
