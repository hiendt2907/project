# shellcheck shell=bash
# Probe clusters: OrbStack → ~/.kube default ctx → $KUBECONFIG → kubectl default merge.

kube_resolve_cache_file() {
  echo "${TMPDIR:-/tmp}/multi-agent-kube-resolve.cache"
}

kube_resolve_ttl_sec() {
  echo "${OMNI_KUBE_RESOLVE_TTL:-90}"
}

kube_resolve_try() {
  local kcfg="$1"
  local ctx="${2:-}"
  if [[ "$kcfg" == "__DEFAULT__" ]]; then
    (unset KUBECONFIG; kubectl cluster-info --request-timeout=4s &>/dev/null)
    return
  fi
  [[ -n "$kcfg" && -f "$kcfg" ]] || return 1
  if [[ -n "$ctx" ]]; then
    env KUBECONFIG="$kcfg" kubectl --context="$ctx" cluster-info --request-timeout=4s &>/dev/null
  else
    env KUBECONFIG="$kcfg" kubectl cluster-info --request-timeout=4s &>/dev/null
  fi
}

kube_resolve_write_cache() {
  local ts="$1" kcfg="$2" ctx="$3"
  local cache
  cache="$(kube_resolve_cache_file)"
  printf '%s\t%s\t%s\n' "$ts" "$kcfg" "$ctx" >"$cache"
}

kube_resolve_probe() {
  local now ts kcfg ctx
  now=$(date +%s)
  local cache
  cache="$(kube_resolve_cache_file)"
  if [[ -f "$cache" ]]; then
    IFS=$'\t' read -r ts kcfg ctx <"$cache" || true
    if [[ -n "${ts:-}" ]] && (( now - ts < $(kube_resolve_ttl_sec) )) && kube_resolve_try "${kcfg:-}" "${ctx:-}"; then
      printf '%s\t%s\n' "${kcfg:-}" "${ctx:-}"
      return 0
    fi
  fi

  if kube_resolve_try "${HOME}/.kube/config" "orbstack"; then
    kube_resolve_write_cache "$now" "${HOME}/.kube/config" "orbstack"
    printf '%s\t%s\n' "${HOME}/.kube/config" "orbstack"
    return 0
  fi

  if kube_resolve_try "${HOME}/.kube/config" ""; then
    kube_resolve_write_cache "$now" "${HOME}/.kube/config" ""
    printf '%s\t%s\n' "${HOME}/.kube/config" ""
    return 0
  fi

  if [[ -n "${KUBECONFIG:-}" ]]; then
    local first="${KUBECONFIG%%:*}"
    if [[ -f "$first" ]] && kube_resolve_try "$first" ""; then
      kube_resolve_write_cache "$now" "$first" ""
      printf '%s\t%s\n' "$first" ""
      return 0
    fi
  fi

  if kube_resolve_try "__DEFAULT__" ""; then
    kube_resolve_write_cache "$now" "__DEFAULT__" ""
    printf '%s\t%s\n' "__DEFAULT__" ""
    return 0
  fi

  return 1
}
