#!/usr/bin/env bash
set -euo pipefail

# End-to-end shadow loop smoke for a disk-style incident workflow:
# 1) inject chaos credential fault (real crashloop path),
# 2) emit local shadow feedback via CLI,
# 3) verify analyst consumed feedback transitions,
# 4) restore workload baseline.

TRACE_ID="${TRACE_ID:-shadow-disk-$(date +%s)}"
BOOTSTRAP="${BOOTSTRAP:-kafka:9092}"
TOPIC="${TOPIC:-omni-action-feedback}"
NAMESPACE="${NAMESPACE:-multi-agent}"

echo "[shadow-loop] trace_id=${TRACE_ID}"

cleanup() {
  echo "[shadow-loop] restore baseline..."
  bash scripts/inject_real_fault.sh --restore >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[shadow-loop] inject fault..."
if ! bash scripts/inject_real_fault.sh; then
  echo "[shadow-loop] WARN: inject_real_fault.sh returned non-zero; continuing shadow feedback validation"
fi

echo "[shadow-loop] run local mock generator..."
bash scripts/mock_shadow_os_errors.sh diskpressure "${TRACE_ID}"

echo "[shadow-loop] publish command feedback..."
EXECUTOR_POD="$(./scripts/with_working_kube.sh kubectl -n "${NAMESPACE}" get pods -l app=omni-fullstack -o jsonpath='{.items[0].metadata.name}')"
if [[ -z "${EXECUTOR_POD}" ]]; then
  echo "[shadow-loop] ERROR: cannot find omni-fullstack pod"
  exit 1
fi
./scripts/with_working_kube.sh kubectl -n "${NAMESPACE}" exec "${EXECUTOR_POD}" -- \
  python /app/scripts/omni_shadow_exec_feedback.py \
    --trace-id "${TRACE_ID}" \
    --step-id diskpressure-step1 \
    --dry-run-command "echo dry-run disk check ok" \
    --command "df -h | head -n 8" \
    --timeout-sec 30 \
    --kafka-bootstrap "${BOOTSTRAP}" \
    --kafka-topic "${TOPIC}"

echo "[shadow-loop] verify analyst consumed trace..."
if ./scripts/with_working_kube.sh logs -n "${NAMESPACE}" deploy/omni-fullstack --since=10m | grep -q "${TRACE_ID}"; then
  echo "[shadow-loop] analyst trace observed: ${TRACE_ID}"
else
  echo "[shadow-loop] ERROR: analyst trace not found: ${TRACE_ID}"
  exit 1
fi

if ./scripts/with_working_kube.sh logs -n "${NAMESPACE}" deploy/omni-fullstack --since=10m | grep -q "COMMAND_FEEDBACK_INGESTED"; then
  echo "[shadow-loop] feedback transition observed"
else
  echo "[shadow-loop] ERROR: COMMAND_FEEDBACK_INGESTED not observed"
  exit 1
fi

echo "[shadow-loop] DONE"
