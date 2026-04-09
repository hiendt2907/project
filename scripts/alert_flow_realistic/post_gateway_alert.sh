#!/usr/bin/env bash
# POST Alertmanager-style JSON tới omni-gateway (in-cluster), in trace_id như gateway_alert_loki_verify.sh.
# Usage: post_gateway_alert.sh path/to/payload.json
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"
NS="${NS:-multi-agent}"
GW_INTERNAL="${GW_INTERNAL:-http://omni-gateway.multi-agent.svc.cluster.local}"
EXEC_DEPLOY="${E2E_EXEC_DEPLOY:-omni-prober}"
PAYLOAD="${1:?usage: $0 path/to/payload.json}"

if [[ ! -f "$PAYLOAD" ]]; then
  echo "Missing file: $PAYLOAD" >&2
  exit 1
fi

RESP="$("${KUBE}" exec -i -n "$NS" "deploy/${EXEC_DEPLOY}" -- python3 -c "
import json, sys, urllib.request
body = sys.stdin.buffer.read()
req = urllib.request.Request(
    '${GW_INTERNAL}/webhook/prometheus',
    data=body,
    headers={'Content-Type': 'application/json'},
    method='POST',
)
print(urllib.request.urlopen(req, timeout=45).read().decode())
" < "$PAYLOAD")"
echo "$RESP"
TRACE="$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('trace_id',''))" 2>/dev/null || true)"
echo "trace_id=$TRACE"
