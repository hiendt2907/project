#!/usr/bin/env bash
# Lab: gateway alert → trace → synthetic omni-action-feedback → grep analyst death-loop markers.
#
# Usage:
#   NS=<ns> bash scripts/e2e_death_loop_lab_complete.sh
#   NS=<ns> bash scripts/e2e_death_loop_lab_complete.sh '<existing_trace_id>'
#
# Requires: kubectl, cluster with omni-gateway + omni-analyst + kafka; Python with aiokafka
#   (default: repo .venv — override with PYTHON=/path/to/python).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"
if [[ -z "${NS:-}" ]]; then
  echo "e2e_death_loop_lab_complete.sh: set NS (no default)." >&2
  exit 2
fi
TRACE="${1:-}"
PY="${PYTHON:-${ROOT}/.venv/bin/python}"
if [[ ! -x "${PY}" ]]; then
  PY="python3"
fi

if [[ -z "$TRACE" ]]; then
  echo "=== 1) gateway_alert_loki_verify (capture trace_id) ==="
  OUT="$(cd "${ROOT}" && NS="${NS}" bash scripts/gateway_alert_loki_verify.sh 2>&1)" || true
  echo "$OUT" | tail -n 80
  TRACE="$(echo "$OUT" | grep -E '^trace_id=' | head -1 | cut -d= -f2- || true)"
  if [[ -z "$TRACE" ]]; then
    echo "FAIL: no trace_id= from gateway script; pass trace as arg1 or fix gateway/redis." >&2
    exit 1
  fi
fi

echo ""
echo "=== 2) Publish synthetic omni-action-feedback trace=${TRACE} ==="
export E2E_OMNI_KUBE_NAMESPACE="${NS}"
export E2E_KUBECTL_WRAPPER="${KUBE}"
"${PY}" "${ROOT}/scripts/e2e_lab_publish_synthetic_feedback.py" --trace-id "${TRACE}"

echo ""
echo "=== 3) Wait for analyst consumer (adjust if cluster slow) ==="
sleep "${E2E_FEEDBACK_WAIT_SEC:-12}"

echo ""
echo "=== 4) Collect trace evidence (expect analyst COMMAND_FEEDBACK_INGESTED >= 1) ==="
EVID="$(bash "${ROOT}/scripts/e2e_collect_trace_evidence.sh" "${TRACE}")"
echo "$EVID"
FB_ING="$(echo "$EVID" | grep -E '^count_command_feedback_ingested=' | head -1 | cut -d= -f2- | tr -d ' ')"
FB_RX="$(echo "$EVID" | grep -E '^count_action_feedback_received=' | head -1 | cut -d= -f2- | tr -d ' ')"
if [[ "${FB_ING:-0}" =~ ^[0-9]+$ ]] && [[ "${FB_ING}" -ge 1 ]]; then
  echo "PASS: command feedback ingested (death-loop channel) count=${FB_ING}"
elif [[ "${FB_RX:-0}" =~ ^[0-9]+$ ]] && [[ "${FB_RX}" -ge 1 ]]; then
  echo "PASS: action_feedback_received in logs count=${FB_RX}"
else
  echo "FAIL: no COMMAND_FEEDBACK_INGESTED / action_feedback_received for trace=${TRACE} (scale omni-analyst, Kafka group, or E2E_FEEDBACK_WAIT_SEC)." >&2
  exit 1
fi

echo ""
echo "Done."
