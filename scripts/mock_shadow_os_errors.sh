#!/usr/bin/env bash
set -euo pipefail

# Lab-safe mock error generator for Shadow OS testing.
# Uses resource-limited containers/loopback files only.

CASE_ID="${1:-}"
TRACE_ID="${2:-mock-trace}"

if [[ -z "${CASE_ID}" ]]; then
  echo "usage: $0 <diskpressure|oomkilled|app5xx> [trace_id]"
  exit 2
fi

echo "mock_case_id=${CASE_ID} trace_id=${TRACE_ID}"

case "${CASE_ID}" in
  diskpressure)
    # Loopback file simulation (does not pressure host root fs).
    TMP_IMG="/tmp/omni-mock-disk-${TRACE_ID}.img"
    dd if=/dev/zero of="${TMP_IMG}" bs=1m count=64 status=none
    echo "created_loopback_image=${TMP_IMG}"
    ;;
  oomkilled)
    # Container-limited memory stress (safe boundary).
    docker run --rm --memory=128m --cpus=0.5 alpine:3.20 sh -lc 'x=; while true; do x="$x$(head -c 1048576 </dev/zero | tr "\0" "a")"; done' || true
    echo "oom_simulated_with_container_limit=true"
    ;;
  app5xx)
    # Synthetic app-log signal for app_log lane tests.
    for i in $(seq 1 20); do
      echo "{\"trace_id\":\"${TRACE_ID}\",\"level\":\"error\",\"status\":500,\"msg\":\"mock 5xx surge\"}"
    done
    ;;
  *)
    echo "unknown case: ${CASE_ID}"
    exit 3
    ;;
esac
