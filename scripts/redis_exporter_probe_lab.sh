#!/usr/bin/env bash
# Deploy redis-exporter, break readiness probe (wrong port), reload Prometheus rules,
# POST gateway webhook (ProbeFailureLab), grep trace in logs + optional Loki.
# Restore healthy deployment at end (unless REDIS_EXPORTER_LAB_NO_RESTORE=1).
#
# Usage:
#   NS=<ns> bash scripts/redis_exporter_probe_lab.sh
# Env:
#   KUBE_NS= / NS=              **required** — redis-exporter + omni workload namespace (no default)
#   REDIS_EXPORTER_LAB_NO_RESTORE=1   # keep broken deployment for manual inspection
#   SLEEP_SEC=35                      # passed to gateway_alert_loki_verify.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"
NS="${KUBE_NS:-${NS:-}}"
if [[ -z "${NS}" ]]; then
  echo "redis_exporter_probe_lab.sh: set KUBE_NS or NS (no default)." >&2
  exit 2
fi
export NS
MON_NS="${MONITOR_NS:-monitor}"

echo "=== [1/6] Apply redis-exporter (healthy) ==="
"${KUBE}" apply -f "${ROOT}/k8s/monitor/redis-exporter.yaml"
"${KUBE}" rollout status deployment/redis-exporter -n "$NS" --timeout=180s

POD="$("${KUBE}" get pods -n "$NS" -l app=redis-exporter -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -z "${POD}" ]]; then
  echo "FAIL: no redis-exporter pod in $NS" >&2
  exit 1
fi
echo "pod=$POD"

echo ""
echo "=== [2/6] Break readiness: httpGet port 9999 (metrics still on 9121) ==="
"${KUBE}" patch deployment redis-exporter -n "$NS" --type=json -p='[
  {"op": "replace", "path": "/spec/template/spec/containers/0/readinessProbe/httpGet/port", "value": 9999}
]'
sleep 8
"${KUBE}" get pods -n "$NS" -l app=redis-exporter -o wide || true

echo ""
echo "=== [3/6] Apply Prometheus ConfigMap (ProbeFailureLab rule) + reload ==="
"${KUBE}" apply -f "${ROOT}/k8s/monitor/prometheus.yaml"
if "${KUBE}" get pod -n "$MON_NS" prometheus-0 &>/dev/null; then
  "${KUBE}" exec -n "$MON_NS" prometheus-0 -- wget -qO- --post-data="" "http://127.0.0.1:9090/-/reload" || true
  echo "prometheus reload sent"
else
  echo "WARN: prometheus-0 not in $MON_NS — apply rules manually and reload."
fi

echo ""
echo "=== [4/6] Build alert JSON with real pod name ==="
TMP_JSON="$(mktemp)"
sed "s/PLACEHOLDER_POD/${POD}/g" "${ROOT}/scripts/alert_payloads/alertmanager_redis_exporter_probe.json" > "$TMP_JSON"
echo "wrote $TMP_JSON"

echo ""
echo "=== [5/6] Gateway + Loki trace (see RAG_CHUNK / SUGGEST_REMEDIATION / KNOWLEDGE_UNCERTAIN) ==="
SLEEP_SEC="${SLEEP_SEC:-35}" bash "${ROOT}/scripts/gateway_alert_loki_verify.sh" "$TMP_JSON" || true
rm -f "$TMP_JSON"

echo ""
echo "=== [6/6] Restore redis-exporter readiness (port 9121) ==="
if [[ "${REDIS_EXPORTER_LAB_NO_RESTORE:-0}" == "1" ]]; then
  echo "REDIS_EXPORTER_LAB_NO_RESTORE=1 — skip restore."
  exit 0
fi
"${KUBE}" patch deployment redis-exporter -n "$NS" --type=json -p='[
  {"op": "replace", "path": "/spec/template/spec/containers/0/readinessProbe/httpGet/port", "value": 9121}
]'
"${KUBE}" rollout status deployment/redis-exporter -n "$NS" --timeout=180s
echo "OK: redis-exporter readiness restored."

echo ""
echo "=== Executor pre_apply_revalidate_ok (manual) ==="
echo "Revalidate runs only on k8s_rollout_restart after Telegram confirm (Redis pending + evidence_snapshot)."
echo "Watch omni-fullstack logs when confirming rollout: event=pre_apply_revalidate_ok"
echo "Or: kubectl logs -n $NS deploy/omni-fullstack --tail=200 | grep pre_apply_revalidate_ok"
