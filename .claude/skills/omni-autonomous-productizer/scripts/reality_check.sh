#!/usr/bin/env bash
# Read-only Reality Map probe. Never mutates anything — safe to run at the
# start of every start/resume/status invocation. Missing tools are reported,
# not fatal (this script must work even without kubectl/orb available).
set -Eeuo pipefail

log() { printf '[reality_check] %s\n' "$*"; }

log "=== git ==="
git status --short 2>&1 || log "git status failed"
git branch --show-current 2>&1 || true
git rev-parse HEAD 2>&1 || true
git log --oneline --decorate -20 2>&1 || true
git diff --stat 2>&1 || true

log "=== kubectl ==="
if command -v kubectl >/dev/null 2>&1; then
  kubectl get deploy,pod,svc -A -o wide 2>&1 || log "kubectl get failed (cluster unreachable?)"
  kubectl get events -A --sort-by=.lastTimestamp 2>&1 | tail -30 || true
else
  log "kubectl not found on PATH"
fi

log "=== orb ==="
if command -v orb >/dev/null 2>&1; then
  orb status 2>&1 || log "orb status failed"
  orb list 2>&1 || log "orb list failed"
else
  log "orb not found on PATH"
fi

log "=== auto-execute safety check ==="
if command -v kubectl >/dev/null 2>&1; then
  kubectl get deploy -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"/"}{.metadata.name}{": "}{range .spec.template.spec.containers[*].env[?(@.name=="OMNI_AUTO_EXECUTE_ENABLED")]}{.value}{end}{"\n"}{end}' 2>&1 \
    | grep -i "OMNI\|true\|false" || log "no OMNI_AUTO_EXECUTE_ENABLED env found in any deployment (may be set via ConfigMap)"
fi

log "=== done — this output is read-only, no mutation performed ==="
