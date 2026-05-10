#!/usr/bin/env bash
# E2E: nginx-test thiếu ConfigMap — patch envFrom rồi xóa ConfigMap → CreateContainerConfigError (fault thật).
# Không dùng đường CPU “alert nóng / PodMetrics ~0” (false negative).
#
# Usage:
#   NS=<ns> bash scripts/e2e_nginx_missing_configmap.sh
# Env:
#   NS=                  **required**
#   SLEEP_SEC=120
#   STRICT_ASSERT=1  — cần trace trong ≥3 deploy + action markers; đường broken_spec+RAG intercept đôi khi chỉ prober+analyst (không kafka tới executor).
#   Mặc định STRICT_ASSERT=0 để gate “fault thật + pipeline” không false fail.
#   E2E_ASSERT_DIAGNOSTIC_POLICY=1 — grep planner/invariant trong log worker (cần analyst có trace trong tail; đôi khi chỉ prober khớp).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -z "${NS:-}" ]]; then
  echo "e2e_nginx_missing_configmap.sh: set NS to the Kubernetes namespace (no default)." >&2
  exit 2
fi
export SCENARIOS=nginx_waiting_fault
export SLEEP_SEC="${SLEEP_SEC:-120}"
export STRICT_ASSERT="${STRICT_ASSERT:-0}"
export E2E_ASSERT_DIAGNOSTIC_POLICY="${E2E_ASSERT_DIAGNOSTIC_POLICY:-0}"

exec bash "${ROOT}/scripts/e2e_incident_matrix.sh"
