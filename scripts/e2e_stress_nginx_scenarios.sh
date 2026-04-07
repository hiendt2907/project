#!/usr/bin/env bash
# E2E stress scenarios — nginx-test in multi-agent (real cluster).
# Plan: CREATE_CONTAINER_ERROR | CRASH_LOOP (log previous) | OOM + optional auto-repair.
#
# Prerequisites: Deployment nginx-test exists; Alertmanager fires kube-state alerts with
# labels namespace, pod, reason; omni worker/prober/analyst/executor deployed; Loki collects pod logs.
#
# Usage:
#   bash scripts/e2e_stress_nginx_scenarios.sh help
#   RUN=1 bash scripts/e2e_stress_nginx_scenarios.sh 1   # apply scenario 1 patch
#   RUN=1 bash scripts/e2e_stress_nginx_scenarios.sh restore
#
# Loki (replace TRACE with your trace_id from gateway/alert):
#   {namespace="multi-agent"} |= "TRACE" |= "diagnostic_evidence_publish"
#   {namespace="multi-agent"} |= "TRACE" |= "k8s_clinical_pod_log_previous"
#   {namespace="multi-agent"} |= "TRACE" |= "k8s_log_previous"
#
# RAG fallback (analyst): grep EMERGENCY_ZERO_RAG | ollama_embed | 400 in logs for that trace.
#
# OOM auto-repair: set OMNI_AUTO_EXECUTE_ENABLED=true on executor; watch:
#   {namespace="multi-agent"} |= "TRACE" |= "action_feedback_published"
#   topic omni-action-feedback (Kafka) same trace_id.
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"
NS="${NS:-multi-agent}"
DEPLOY="${DEPLOY:-nginx-test}"
MANIFEST="${ROOT}/scripts/nginx-test-deployment.yaml"

scen1_patch() {
  "${KUBE}" patch deployment "${DEPLOY}" -n "${NS}" -p '{"spec":{"template":{"spec":{"containers":[{"name":"nginx","envFrom":[{"configMapRef":{"name":"non-existent-config"}}]}]}}}}'
}

scen2_patch() {
  "${KUBE}" patch deployment "${DEPLOY}" -n "${NS}" -p '{"spec":{"template":{"spec":{"containers":[{"name":"nginx","command":["sh","-c","exit 1"]}]}}}}'
}

scen3_patch() {
  # Docker/OrbStack often rejects limits.memory < 6MB ("Minimum memory limit allowed is 6MB").
  # Requests must be <= limits; tune for your runtime to reach OOMKilled vs CreateContainerError.
  "${KUBE}" patch deployment "${DEPLOY}" -n "${NS}" -p '{"spec":{"template":{"spec":{"containers":[{"name":"nginx","resources":{"requests":{"cpu":"10m","memory":"4Mi"},"limits":{"memory":"6Mi"}}}]}}}}'
}

restore() {
  echo "Restoring ${DEPLOY} from ${MANIFEST}"
  "${KUBE}" apply -f "${MANIFEST}"
  # Strategic merge can leave envFrom/command from prior lab patches — strip if present.
  "${KUBE}" patch deployment "${DEPLOY}" -n "${NS}" --type=json \
    -p='[{"op":"remove","path":"/spec/template/spec/containers/0/envFrom"}]' 2>/dev/null || true
  "${KUBE}" patch deployment "${DEPLOY}" -n "${NS}" --type=json \
    -p='[{"op":"remove","path":"/spec/template/spec/containers/0/command"}]' 2>/dev/null || true
  "${KUBE}" rollout status "deployment/${DEPLOY}" -n "${NS}" --timeout=120s || true
}

help() {
  cat <<EOF
E2E stress — nginx-test (${NS})

Expected probes (after smart dispatcher rollout):
  Scenario 1 (bad ConfigMap): k8s_clinical_pod_events + k8s_resource_quota_probe (no log_tail tier-2).
  Scenario 2 (exit 1 / CrashLoop): k8s_clinical_pod_log_previous (k8s_log_previous=true) + k8s_clinical_pod_events.
  Scenario 3 (2Mi OOM): tier-2 focuses on PodMetrics + prom memory when status reports OOMKilled.

Run patches (destructive — lab only):
  RUN=1 $0 1
  RUN=1 $0 2
  RUN=1 $0 3
Restore baseline:
  RUN=1 $0 restore

Manual verification checklist (same trace_id across pods):
  1) Loki: diagnostic_evidence_publish for probes above.
  2) RAG: if embed fails, expect EMERGENCY_ZERO_RAG — do not require RAG_CHUNK citation.
  3) OOM + auto: EXECUTE_MUTATE + omni-action-feedback with matching trace_id (requires OMNI_AUTO_EXECUTE_ENABLED=true).
EOF
}

main() {
  local cmd="${1:-help}"
  if [[ "${cmd}" == "help" || "${cmd}" == "-h" ]]; then
    help
    return 0
  fi
  if [[ "${RUN:-0}" != "1" ]]; then
    echo "Set RUN=1 to execute kubectl patches or restore. Preview:"
    case "${cmd}" in
      1) echo "Would run: scen1_patch (ConfigMap ref)" ;;
      2) echo "Would run: scen2_patch (exit 1)" ;;
      3) echo "Would run: scen3_patch (2Mi memory)" ;;
      restore) echo "Would run: restore (apply manifest)" ;;
      *) help; return 1 ;;
    esac
    return 0
  fi
  case "${cmd}" in
    1) scen1_patch ;;
    2) scen2_patch ;;
    3) scen3_patch ;;
    restore) restore ;;
    *) help; return 1 ;;
  esac
}

main "$@"
