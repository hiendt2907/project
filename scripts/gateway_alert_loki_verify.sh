#!/usr/bin/env bash
# POST alert Alertmanager (pod+namespace) vào gateway → trace_id → logs pod + Loki.
#
# Master Plan V3 (split): dùng deploy có Pod sẵn (mặc định omni-prober) để POST nội bộ;
# gom log từ omni-prober / omni-analyst / omni-core / omni-executor (+ omni-worker nếu scale > 0).
#
# Usage: scripts/gateway_alert_loki_verify.sh [path/to/alert.json]
# Env:
#   LOKI_URL=http://loki.monitor.svc.cluster.local:3100
#   SLEEP_SEC=25
#   E2E_EXEC_DEPLOY=omni-prober          # Pod chạy python3 để POST tới gateway
#   E2E_TRACE_LOG_DEPLOYS="omni-prober …" # override danh sách grep log theo trace
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"
PAYLOAD="${1:-${ROOT}/scripts/alert_payloads/alertmanager_business_sane.json}"
GW_INTERNAL="${GW_INTERNAL:-http://omni-gateway.multi-agent.svc.cluster.local}"
LOKI_URL="${LOKI_URL:-http://loki.monitor.svc.cluster.local:3100}"
SLEEP_SEC="${SLEEP_SEC:-25}"
NS="${NS:-multi-agent}"

# Pod có Python + image worker — tránh omni-gateway (slim) nếu thiếu python.
EXEC_DEPLOY="${E2E_EXEC_DEPLOY:-omni-prober}"

_default_trace_deploys() {
  local base="omni-prober omni-analyst omni-core omni-executor"
  local r
  r="$("${KUBE}" get deploy omni-worker -n "$NS" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
  if [[ "${r:-0}" != "0" ]]; then
    echo "omni-worker $base"
  else
    echo "$base"
  fi
}

TRACE_LOG_DEPLOYS="${E2E_TRACE_LOG_DEPLOYS:-$(_default_trace_deploys)}"

if [[ ! -f "$PAYLOAD" ]]; then
  echo "Missing payload: $PAYLOAD" >&2
  exit 1
fi

if ! "${KUBE}" get deploy "$EXEC_DEPLOY" -n "$NS" &>/dev/null; then
  echo "FAIL: no deployment/$EXEC_DEPLOY in $NS (set E2E_EXEC_DEPLOY to a running worker pod, e.g. omni-prober)." >&2
  exit 1
fi
r_exec="$("${KUBE}" get deploy "$EXEC_DEPLOY" -n "$NS" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
if [[ "${r_exec:-0}" == "0" ]]; then
  echo "FAIL: deployment/$EXEC_DEPLOY has replicas 0 — scale up or set E2E_EXEC_DEPLOY." >&2
  exit 1
fi

echo "=== 0) Topology: exec deploy=$EXEC_DEPLOY | trace logs: $TRACE_LOG_DEPLOYS ==="

echo "=== 1) POST ${GW_INTERNAL}/webhook/prometheus ==="
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

TRACE="$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('trace_id',''))" 2>/dev/null || true)"
if [[ -z "$TRACE" ]]; then
  echo "Response: $RESP" >&2
  echo "FAIL: no trace_id (circuit breaker / redis / gateway error)." >&2
  exit 1
fi
echo "trace_id=$TRACE"
echo ""

echo "=== 2) omni-gateway logs (enqueue) ==="
"${KUBE}" logs -n "$NS" deploy/omni-gateway --tail=200 2>/dev/null | grep -F "$TRACE" || echo "(no line — tail nhỏ hoặc replica khác)"

echo ""
echo "=== 3) Chờ xử lý (${SLEEP_SEC}s) rồi grep trace trong log split workers ==="
sleep "$SLEEP_SEC"
WR_LINES=""
for dep in $TRACE_LOG_DEPLOYS; do
  r="$("${KUBE}" get deploy "$dep" -n "$NS" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
  if [[ "${r:-0}" == "0" ]]; then
    continue
  fi
  chunk="$("${KUBE}" logs -n "$NS" "deploy/${dep}" --since=15m --tail=4000 2>/dev/null | grep -F "$TRACE" || true)"
  if [[ -n "$chunk" ]]; then
    WR_LINES+="--- deploy/${dep} ---
${chunk}
"
  fi
done
if [[ -z "$WR_LINES" ]]; then
  echo "(no lines in omni-prober/analyst/core/executor[/worker] — tăng SLEEP_SEC hoặc kiểm tra consumer Kafka)"
else
  echo "$WR_LINES" | tail -n 120
  echo ""
  echo "--- grep handler_done (gợi ý kết quả nghiệp vụ) ---"
  echo "$WR_LINES" | grep -F "handler_done" || echo "(chưa có handler_done — pipeline còn xử lý hoặc chỉ stream_consumer)"
fi

echo ""
echo "=== 4) Loki query_range (Promtail: namespace + pod_name) ==="
LOKI_POD_RE='omni-prober.*|omni-analyst.*|omni-core.*|omni-executor.*|omni-gateway.*|omni-worker.*'
echo "LogQL (Grafana Explore): {namespace=\"multi-agent\", pod_name=~\"${LOKI_POD_RE}\"} |= \"$TRACE\""
"${KUBE}" exec -n "$NS" "deploy/${EXEC_DEPLOY}" -- python3 -c "
import json, time, urllib.parse, urllib.request
trace = '''${TRACE}'''
loki = '''${LOKI_URL}'''
q = '{namespace=\"multi-agent\", pod_name=~\"${LOKI_POD_RE}\"} |= \"' + trace + '\"'
now = int(time.time())
start = (now - 3600) * 10**9
end = now * 10**9
params = urllib.parse.urlencode({'query': q, 'limit': '100', 'start': str(start), 'end': str(end)})
url = loki.rstrip('/') + '/loki/api/v1/query_range?' + params
try:
    r = urllib.request.urlopen(url, timeout=25)
    d = json.loads(r.read().decode())
except Exception as e:
    print(json.dumps({'loki_error': str(e)}))
    raise SystemExit(0)
res = d.get('data', {}).get('result') or []
lines = []
for s in res:
    for v in s.get('values') or []:
        if len(v) >= 2:
            lines.append(v[1])
print('--- Loki (last 35 lines) ---')
for ln in lines[-35:]:
    print((ln[:600] + '…') if len(ln) > 600 else ln)
if not lines:
    print('(empty — kiểm tra Promtail ship namespace multi-agent / Loki DNS)')
"

echo ""
echo "=== 5) Checklist nghiệp vụ ==="
echo "• Inbound phải có pod= + namespace= trong FACTS (alert JSON có labels)."
echo "• MPV3 split: trace xuất hiện trước hết ở omni-prober (omni-alerts); analyst = evidence loop."
echo "• Dùng trace trong Grafana Explore Loki:  $TRACE"
