#!/usr/bin/env bash
# kubectl với cluster đầu tiên trả lời: OrbStack → ~/.kube mặc định → $KUBECONFIG → merge mặc định.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/lib/kube_resolve.sh"

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
